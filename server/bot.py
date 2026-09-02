#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""房间 bot —— V0.3。

人少的时候房主在**聊天窗口**敲一条命令就能加一个 bot 陪打：它在房间里占一个
座位、有昵称和 3D 模型、能换角色 / 换队伍 / 一键准备 / 删除。

## 这个模块和 `gameserver.py` 怎么分工

- **组包发包一律走 `Conn` 的现成方法**（`broadcast_seat_slot` /
  `broadcast_seat_leave` / `send_system_chat` / `broadcast_system_chat`）。
  这里一个 `struct.pack` 都不写 —— 线格式的考据全在 `gameserver.py` 里，
  再抄一份迟早两边长歪（CLAUDE.md 铁律 8 的道理）。
  战斗中那份同步数据的线格式在 `botsync.py`，同一个道理。
- **bot 的每座位状态直接住在 `lobby.Seat` 里**（昵称 / 角色 / 队伍 / 准备
  + 新加的 `is_bot`），不另立一份 bot 座位表（D9）。
  只属于「它那台机器」的状态（同步流、落脚点、朝向）住在 `BotConn` 上。

## 战斗中（M3）

`tick_room()` 是 bot 的**帧循环**，由房里真人的同步包到达驱动
（`gameserver._relay_battle_tick` -> `BOT_ROOM_TICK`，D17）。
每一帧算出落脚点，交给 `BotConn.sync`（`botsync.BotSyncStream`）合成
`UdpPacket`，再走现成的 `relayserver.deliver()` 投出去。

★ 落脚点是**回放真人走过的轨迹**（D16）：服务端一点地图几何都没有
（M4 才有），真人刚站过的点一定是合法地面，连跳跃的抛物线都是现成的。

## 导入方向（★ 别反过来）

`bot.py` **可以** `import gameserver`（`BotConn` 要拿 `Conn` 当基类）；
`gameserver.py` 反过来只在 `on_chat()` 里**函数内**惰性 `import bot`，
战斗帧那条路则靠本模块 import 时把 `tick_room` 挂到 `gameserver.BOT_ROOM_TICK`。
两边都写成模块级 import 就是循环导入 —— `class BotConn(gameserver.Conn)`
会在 `gameserver` 才执行到一半、`Conn` 还没定义的时候炸掉。

## 相关决定

- D1 —— 为什么是「假连接对象」而不是 `Seat.conn = None`；
- D2 —— 房主迁移为什么跳过 bot 座；
- D6 —— `/c N M` 的 M 为什么是面板序号 1..14 而不是原始角色 id；
- D7 —— bot 占座时真人为什么照常被「房间已满」挡住；
- D16 —— bot 的落脚点为什么是「回放真人的轨迹」而不是自己算；
- D17 —— bot 的帧为什么由真人的同步包驱动，而不是定时器线程。
"""
from __future__ import annotations

import collections
import contextlib
import math
import os
import random
import re
import struct
import threading
import time
import weakref

from account_store import BASE_CHARACTER_IDS, MINIMUM_PLAYER_LEVEL, \
    PREMIUM_CHARACTER_IDS
import asynclog
import ballistics
import botaim
import botarms
import botbreak
import bothp
import botmove
import botnav
import botplan
import botsync
import botthreat
import chrprops
import gameserver
import lobby as lobby_module
import mapdata
import relayserver
import udpsync
import weapondata
from lobby import ROOM_SEAT_COUNT, Seat, TEAM_A, TEAM_B, TEAM_LAYOUT_TEAMS

#: bot 的昵称：`bot <座位号>`。
#:
#: ★ 座位号用的是**服务端座位号 0..5**，和日志、`0x0301` 的座位字段、
#: `/c N M` / `/t N` 里的 N 完全一致 —— 玩家在聊天窗口看到「bot 3」，
#: 敲的就是 `/c 3 2`，不用在脑子里做任何换算。
BOT_NICKNAME_PREFIX = "bot "

#: bot 座位里发下去的等级（`SessionSlot+0x10`）。
#:
#: 房间里按「开始」时客户端只读**房主座位**的等级（V0.1 §77），bot 永远不是
#: 房主，所以这个数不影响任何准入判定 —— 它纯粹是玩家列表里显示的那一格。
#: 取 `MINIMUM_PLAYER_LEVEL`（真人下发等级的下限）是为了看起来不突兀。
BOT_LEVEL = MINIMUM_PLAYER_LEVEL

#: 「人物选择」面板上的角色顺序 —— `/c N M` 里的 M 就是这张表的 1-based 下标。
#:
#: 原始角色 id 是 `0/1/2` + `100..110`，中间断了一大截（id 3 아이린 和 98
#: 쉐도우 타이 被客户端的按钮循环 `0x4f58e8` 显式跳过，99 랜덤 要另一个开关，
#: 三个都放不出来，见 `account_store.PREMIUM_CHARACTER_IDS` 的注释）。
#: 直接让玩家写原始 id 的话 `/c 3 5` 这种自然写法就是非法值 —— 所以命令里
#: 用连续的面板序号，只在服务端换算一次（D6）。
#:
#: ★ 这张表是**真人**的选择面板，`panel_for_character()` 拿它显示序号。
CHARACTER_PANEL_IDS = tuple(BASE_CHARACTER_IDS) + tuple(PREMIUM_CHARACTER_IDS)

#: ★★★ **bot 只准用初期那三个角色**（`0 / 1 / 2`，用户 2026-08-27 拍板，D54）。
#:
#: 「每个角色的武器属性用法都不相同，每一个都按照角色特性来适配让 bot 会用，
#: 这样感觉太费时间精力了。我改变主意了，bot 可以选择的只有初期的 3 个角色，
#: 商城角色不可以选择作为 bot。」
#:
#: 商城角色的 11 把武器里有反弹弹、炮台、等离子炮这些服务端还没有飞行模型的
#: 类（§72），逐个适配的代价和收益不成比例。三个基础角色的 **9 把武器
#: 全部可用**，而且它们正好覆盖了这个游戏的三类玩法：
#:
#:     角色 0  手枪 / 分裂手雷 / 狙击枪
#:     角色 1  双散弹 / 燃烧瓶 / 追踪火箭
#:     角色 2  机枪 / 抛物线榴弹 / 火箭筒
BOT_CHARACTER_PANEL_IDS = tuple(BASE_CHARACTER_IDS)

#: 新 bot 的默认角色：按座位号在三个基础角色之间轮换。
#:
#: 全给同一个角色的话，三个 bot 在房间里是三个一模一样的模型，谁也认不出谁。
#: 轮换是**确定性**的（同一个座位号永远同一个角色），所以单测能钉住。
BOT_DEFAULT_CHARACTERS = tuple(BASE_CHARACTER_IDS)

#: bot 命令的前缀。普通聊天不会以它开头，所以不会误吞玩家的话。
COMMAND_PREFIX = "/"

#: ★★ 三档 AI 难度只控制用户指定的两种失误（V0.3 M5）：
#:
#: * `aim_error`：计算移动目标提前量时故意算错的概率；
#: * `dodge_error`：预测敌方弹道时故意判断错的概率。
#:
#: 其余物理、武器属性和寻路能力三档完全共用，避免“简单难度”靠违反游戏
#: 规则来变笨。数值是 AI 设计参数，不冒充原版常量；集中在这里便于实机调优。
#:
#: ## ★★ 会话 41：三档 `aim_error` 整体 **+0.15**（用户 2026-08-30：「开枪
#: 准确度还是太高了，三个难度的失误率再整体都调高一点点」）
#:
#: 校准的锚是**语料里真人自己的命中率**（§49）：4533 对几何配对的
#: `rpFire`/`rpExplode` 里，**打中人的只有 1990 对 = 43.9%** —— 也就是说
#: 真人**每 10 发有 5.6 发是打空的**。
#:
#: 而 bot 不掷失误的那一发是「弹道解得精确、提前量也解对了」，除非目标
#: 临时变向否则基本必中 ⇒ 旧的中等档 0.22 相当于命中率 ~78%，**比真人高
#: 一大截**，实机手感就是「隔着老远也弹无虚发」。
#:
#: 取值：中等 **0.40** —— 命中率约 60%，比真人的 44% **稍微聪明一点**
#: （用户要的就是「适中又稍微偏聪明」）；简单 / 困难跟着平移同样的
#: +0.15，保持三档之间原有的区分度。
#: ⚠ `dodge_error` 这一轮**没动**（用户没提，而且它管的是躲不躲得开，
#:   和「准不准」是两件事）。
BOT_DIFFICULTY_PROFILES = {
    "easy": {"aim_error": 0.60, "dodge_error": 0.55},
    "medium": {"aim_error": 0.40, "dodge_error": 0.30},
    "hard": {"aim_error": 0.23, "dodge_error": 0.12},
}
BOT_DIFFICULTY_LABELS = {
    "easy": "简单（笨）",
    "medium": "中等",
    "hard": "困难（聪明）",
}


# ---------------------------------------------------------------------------
# ★★★ 这一格的时刻（D106）—— 一格之内只许有一个「现在」
# ---------------------------------------------------------------------------
#: 房间那条 32 ms 线程在跑一格之前把这一格的**绝对时刻**放进来，跑完还原。
#:
#: 为什么要有它：一格里的每一次「到点了没有」都必须拿**同一个**时刻去比。
#: 一半用这一格的 `now`、另一半现取挂钟的话，追赶时（`now` 是过去的时刻）
#: 两套判据会互相打架 —— 冷却说「还没到」而蓄力说「早过了」。
#: 而把 `now` 一路传进 `_shell_step()` / `_hostile_targets()` 这些叶子函数
#: 要改十几处签名，收益却全在同一件事上。
#:
#: ★ **线程局部**：每个活跃房间一条线程，各跑各的一格，不会串。
#: ★ 不在任何一格里的场合（单测直接调某个函数、网络线程记输入）退回挂钟 ——
#:   那时候「现在」本来就是挂钟。
_TICK_CLOCK = threading.local()


def _now():
    """此刻。在房间那一格里就是**这一格的时刻**，否则是挂钟。"""
    return getattr(_TICK_CLOCK, "now", None) or time.monotonic()


@contextlib.contextmanager
def _tick_clock(now):
    """把这一格的时刻装进 `_now()`（`tick_room()` 用）。"""
    previous = getattr(_TICK_CLOCK, "now", None)
    _TICK_CLOCK.now = now
    try:
        yield
    finally:
        _TICK_CLOCK.now = previous


def difficulty_profile(room):
    """返回房间当前的两项失误概率；旧/假房间安全退回中等。"""
    level = getattr(room, "bot_difficulty", "medium")
    return BOT_DIFFICULTY_PROFILES.get(
        level, BOT_DIFFICULTY_PROFILES["medium"])

#: ★★ **客户端自己就吃掉的斜杠前缀，起名字时必须避开**（§19）。
#:
#: 聊天发送前，客户端 `0x54e727` 拿输入串挨个和这几个前缀做 `wcsnicmp`
#: （**不区分大小写**，而且**要连后面那个空格一起匹配**），中了就把前缀切掉、
#: 只把剩下的正文发出来，同时把 `0x0305` 的聊天类型改成 1（队伍）或 2（悄悄话）。
#: 也就是说这些命令**根本到不了服务端**：`/team 1` 到我们手里只剩一个 `"1"`。
#:
#: 这就是「`/team` 没反应」的全部原因，所以换队命令叫 `/t`（`/tell ` / `/to `
#: 都比它长，第 3 个字符就分岔，撞不上）。
CLIENT_RESERVED_PREFIXES = ("/team ", "/팀 ", "/say ", "/tell ", "/to ", "/귓 ")

#: 造过几个 bot（只用来给日志编号，和座位号无关）。
_bot_seq = 0
_bot_seq_lock = threading.Lock()


def _next_bot_seq():
    global _bot_seq
    with _bot_seq_lock:
        _bot_seq += 1
        return _bot_seq


def character_for_panel(panel_index, table=None):
    """面板序号 -> 原始角色 id。越界返回 ``None``（调用方要报错）。

    ★ 默认查的是 **bot 能用的那张表**（`BOT_CHARACTER_PANEL_IDS`，只有 1~3）。
    要查真人的完整面板就显式传 `CHARACTER_PANEL_IDS`。
    """
    table = BOT_CHARACTER_PANEL_IDS if table is None else table
    try:
        index = int(panel_index)
    except (TypeError, ValueError):
        return None
    if not 1 <= index <= len(table):
        return None
    return table[index - 1]


def panel_for_character(character_id):
    """原始角色 id -> 面板序号 1..14。表里没有就返回 ``None``。"""
    try:
        return CHARACTER_PANEL_IDS.index(int(character_id)) + 1
    except (TypeError, ValueError):
        return None


def bot_nickname(seat_index):
    return f"{BOT_NICKNAME_PREFIX}{int(seat_index)}"


def default_character_for(seat_index):
    return BOT_DEFAULT_CHARACTERS[int(seat_index) % len(BOT_DEFAULT_CHARACTERS)]


# ----------------------------------------------------------------------------
# 假连接
# ----------------------------------------------------------------------------
class BotConn(gameserver.Conn):
    """坐在座位上的 bot 的「连接」。**没有 socket，`send()` 是空操作。**

    ★ **为什么继承 `Conn` 而不是 duck typing**（D1）：`Conn` 有 100 多个方法，
    我们只覆盖得了想到的那几个。继承之后，**没想到**的那些至少不会
    `AttributeError` 炸掉正在调它的**真人**那条线程 —— bot 出问题最多是
    自己不动，不能连累别人。

    ★ **为什么不调 `Conn.__init__`**：那里面全是 socket、流密码、逐连接日志
    文件和 `register_conn()`。下面这份 `__init__` 是它的**镜像**，
    照着同样的顺序把纯内存字段一个个补上，只跳过需要外部资源的那些。
    改 `Conn.__init__` 时请顺手看一眼这里。
    """

    def __init__(self, seat_index, nickname=None, character_id=None):
        self.seq = _next_bot_seq()
        #: 给日志和 `my_nickname()` 用的那份昵称。真相在 `lobby.Seat.nickname`，
        #: 这里存一份是为了「座位已经被摘掉之后」日志还打得出名字。
        self.nickname = nickname or bot_nickname(seat_index)
        self.my_seat = int(seat_index)
        self.character_id = (default_character_for(seat_index)
                             if character_id is None else int(character_id))
        # -- 下面是 `Conn.__init__` 的镜像 -----------------------------------
        self.sock = None
        self.addr = None
        self.args = None
        self.cin = None
        self.cout = None
        self.buf = bytearray()
        self.got_version = True
        self.client_version = None
        self.version_rejected = False
        self.accounts = None
        self.tickets = None
        #: ★ 恒 `None` / `""`：bot 没有存档。结算、经验、金币那几条路遇到
        #: `account is None` 时必须能走下去（M2 的活）。
        self.account_name = None
        self.account = None
        self.start_game = gameserver.StartGameHandshake(seed=0)
        self.quest_score = 0
        self.quest_success = False
        self.settled = False
        self.last_packet_at = time.time()
        self.peer_relay_on = False
        self.send_broken = False
        self.last_relay_reissue_at = 0.0
        self.peer_data_dumped = False
        self.peer_data_in = 0
        self.peer_data_out = 0
        self.peer_game_id = None
        self.peer_epoch = relayserver.PeerEpoch()
        self.peer_forward_ms = relayserver.RttStats()
        self.peer_gap_ms = relayserver.RttStats()
        self.peer_last_at = None
        self.peer_report_at = time.monotonic()
        self.peer_out_gap_ms = relayserver.RttStats()
        self.peer_out_last_at = None
        self.peer_order = udpsync.HeartbeatOrder()
        self.peer_lock = threading.RLock()
        self.peer_order_epoch = None
        self.login_ticket = ""
        self.deaths_broadcast = 0
        self.respawn_sent = 0
        self.room = None
        self.channel_code = 0
        self.channel_index = 0
        self.last_position = None
        # bot 从来不发同步数据给我们，这两个恒空 —— 补上只是为了让 `Conn`
        # 那边任何一个「所有成员都跑一遍」的循环碰到 bot 时不会 AttributeError。
        self.sync_trail = collections.deque(maxlen=gameserver.SYNC_TRAIL_POINTS)
        self.sync_jumped = 0
        self.solo_quest = gameserver.RoomQuest()
        self.items_created = 0
        self.items_picked = 0
        self.attrs_removed = 0
        self.noisy_seen = set()
        self.ft = self.fb_raw = self.fb_dec = None
        self.send_lock = threading.RLock()
        self.send_queue = None
        self.connected_at = time.monotonic()
        # -- 只属于 bot 的机器状态（D9）--------------------------------------
        #: 这个 bot 的同步流（M3）：序号记账 + 组包，见 `botsync.py`。
        self.sync = botsync.BotSyncStream(self)
        #: 战斗中的落脚点 `(x, y)`，None = 还不知道自己站在哪。
        #: ★ 对战房里第一帧从**地图出生点**取（§91，和真人同一套分配规则）；
        #:   闯关房没有出生点可用时才退回「跟着真人走」那条老路（D16）。
        self.battle_pos = None
        #: ★ 上一帧报出去的落脚点 —— 拿它量自己的走速（`_seat_velocity`）。
        #:   踩在地上时 `Body` 的速度恒为 0（§35），只能靠位置差。
        self.battle_pos_prev = None
        #: ★★ **自己走路时的运动状态**（`botmove.Body`），`None` = 还没接管。
        #:   对战房里第一帧的锚是**这个座位该用的地图出生点**（§91），
        #:   之后就由 `botmove` 在地形上自己推（M5 / §71）。
        #:   闯关房仍然回放真人轨迹（那边要的就是「跟着推进」）。
        self.body = None
        #: ★ 上一帧心跳里报出去的**地面标志**（位域 bit2 = `[char+0x128]`）。
        #:   `None` = 这一局还没报过。别人那台机器上 bot 的这一格就是它，
        #:   而夺分模式的一条 ×0.75 正是按受害者这一格判的（§89）。
        self.on_ground = None
        #: ★★ **下一次站起来要落在哪**（`(x, y)`；`None` = 没有待定的重生）。
        #:   服务端替 bot 补 `0x0419` 时把选好的重生点记在这儿（§91），
        #:   `_tick_bot()` 看到「躺着 -> 站起来」的翻转就把身体挪过去 ——
        #:   两边必须是同一个点，否则客户端把它放在 A、心跳把它拽到 B。
        self.pending_spawn = None
        #: ★★ AI 那一半（`_decide()`）算出来的**走位意图** `(方向, 起跳,
        #: 下落, 冲刺跑)`，以及它是在第几格算的（D106）。物理每 32 ms 走
        #: 一格、决策约 15 Hz —— 中间这几格沿用同一份意图，和 D106 之前
        #: `BOT_DECISION_TICKS` 的口径完全一样。
        self.intent = None
        self.intent_tick = None
        #: ★★ 决策那一半挑中的目标（`_fire_target()` 的三元组）。喂心跳里
        #: 的准星和「该不该扣扳机」；真开枪的那一格会拿**最新位置**重解
        #: 一次弹道（§62），所以这一份旧一点没关系。
        self.aim = None
        #: ★★ **这一组心跳里按过 ↓ 没有**（D106）。
        #:
        #: ↓ 只在心跳的按键掩码里（`rpCrouch` 那种事件包管的是蹲，不是它），
        #: 而心跳 4 格才一发 —— bot 穿单向平台时只按一格 ↓，四格里有三格
        #: 撞不上心跳，收方就永远看不见那一下、不让它掉下去（两边从此错位）。
        #: 真人是**按住**的，所以原版没有这个问题。
        #: ⇒ 这一组里按过就锁住，发心跳时报出去、报完清掉。
        self.down_latch = False
        #: ★★ **欠着一发位置心跳**（D115）。房间循环追赶时那几格不报位置
        #: （报了就是一串挤在几毫秒里，收方看着像瞬移）；被跳过的那一发记在
        #: 这里，追平的那一格一起还。★ 不追赶时心跳的相位一个字没变。
        self.beat_pending = False
        #: ★ M5-B 的逐帧路径执行状态。`botnav.plan()` 返回落脚点边；这里保留
        #: 尚未走完的那一串，目标/地图/身体事实变化时再重算，不按挂钟重算。
        self.nav_path = []
        self.nav_goal = None
        self.nav_started = False
        self.nav_failed = None
        #: ★★★ 递给后台规划线程的那张单子（`botplan.Ticket`，V0.3 §137）。
        #:   A\* 不在真人的转发路径上跑了 —— 这一帧递单、下一帧取结果，
        #:   还没取到就照旧走 `_walk_to()` 的老兜底（朝目标直着走）。
        self.nav_ticket = None
        #: ★★ 这一帧已经跑过一次 `botnav.plan()` 了吗（缓存键 = `frame_seq`，
        #:   和 `dodge_at` 同一个套路）。
        #:
        #:   `_own_step()` 是**逐 tick**问意图的（§120），而「往哪走一条路线」
        #:   是一整帧都成立的决定 —— 逐 tick 重新规划纯属白算，而且一次泛洪
        #:   在冷缓存下要几百毫秒，一帧问 16 次就是几秒（实机日志里同步转发
        #:   `max=4756 ms` 就是它）。★ 判据是「这一帧」这个事件，不是挂钟。
        self.nav_planned_at = None
        #: 后台双路线比较证明这件破坏物是更短捷径上的第一道门。
        self.path_breakable = None
        #: 走到这件物体之前仍可在完整地形上安全执行的 Step 前缀。
        self.path_breakable_prefix = []
        #: ★★★★★ 「不打碎 `path_breakable` 这件东西就哪儿都去不了」
        #:   （V0.3 §172）。后台在**完整地形**上连一格都规划不出来时置起，
        #:   `_breaking_now()` 拿它和「被压住」吃同一条待遇：无条件开打。
        self.path_breakable_only = False
        #: ★ 正在执行的那条边要不要在顶点补一次**二段跳**
        #:   （`botnav.ACTION_DOUBLE_JUMP`）。落地 / 换边就清。
        self.nav_double_jump = False
        #: ★★★ 闯关**牵引绳**的记账（D99）。`leash_lagging` = 此刻算不算
        #:   「掉队了」（日志按这个状态翻转去重）；`leash_mark` = 掉队以来
        #:   自己沿前进轴走到过的**最靠前**的地方；`leash_gap` = 掉队那一刻
        #:   的差距。后两个凑起来判「按着方向键却一步都没往前挪」。
        #:   `leash_anchor` = 掉队那一刻**带头的人**的位置（§141）—— 他又
        #:   往前走满一整屏而我还掉着队，就是「追不上」（极速相同，差距
        #:   冻结），该瞬移了。归队就一起清掉。
        self.leash_lagging = False
        self.leash_mark = None
        self.leash_gap = 0.0
        self.leash_anchor = None
        #: 这一帧是否在按 ↓ 穿单向平台。物理层读 `want_drop`，心跳层把它
        #: 翻成 `botsync.KEY_DOWN`；两边必须来自同一个决定。
        self.move_down = False
        #: ★ 临时诊断（会话 17）：上一次打过的「为什么不开枪」，用来做状态翻转
        #:   去重。跟 `BOT_DIAG_FIRE_ANYWHERE` 一起删。
        self.diag_last_why = ""
        #: ★ 诊断（§162）：上一次打过的「为什么一动不动」，同样按状态翻转去重。
        #:   `None` = 这会儿它在动。初值取一个不可能的哨兵，
        #:   免得开局第一格就白打一行「又动起来了」。
        self.diag_idle_why = ""
        #: ★ 诊断（§164）：上一帧的走位意图来自哪个分支，同样按状态翻转去重。
        self.diag_src = None
        #: 现在朝哪走：`+1` 右 / `-1` 左。
        self.heading = botsync.FACING_RIGHT
        #: ★★ **回放真人轨迹那条退路专用**（D16 + D106）：上一次消费到的是
        #: 谁的第几个轨迹点，以及那一步的方向。
        #:
        #: 为什么要记方向：轨迹点 8 Hz 才换一个，而物理是 32 ms 一格 ——
        #: 四格里有三格位置一动不动。拿「这一格挪了没有」当按键掩码的话，
        #: 心跳里报出去的就是 0，收方既不播走路动画、也不替它走（§39），
        #: 位置只被心跳一格一格地拉过去（那正是 §39 修掉的老症状）。
        self.trail_mark = None
        self.trail_heading = 0
        #: 这一轮加载报过那一发 `0x4005` 了吗（D26）。
        #: `None` = 还没报。报过之后恒为 100，拿它**按状态翻转去重**。
        self.load_progress = None
        #: **哪几个真人**已经用自己的 `0x4005` / 加载完成请求证明过
        #: 「我的加载界面建好了」，从而换来一发确认性 100。
        #:
        #: 关掉原版 TCP 中继后，`0x0400` 和第一发 100 紧挨着
        #: 走同一条游戏 TCP 流；客户端处理 `0x0400` 只是预约换
        #: stage，下一帧才建 LoadingStage，因此那一发可能过早。
        #:
        #: ★ 记的是**连接**不是一个布尔：加载界面是**每台客户端各自**
        #:   建的，只按「整房补过一次」去重的话，界面建得比第一个人晚、
        #:   自己又没发过进度包的那一个仍然会看到 0%。每个真人的第一个
        #:   加载事件各补一发，仍然全是事件驱动、一局最多几发。
        self.load_progress_confirmed = frozenset()
        #: ★ 现在蹲着没有（§41）。蹲**不在心跳里**，只有 `rpCrouch` 那一发
        #:   事件包说得着，所以这边得自己记着状态、**只在翻转时发**。
        self.crouched = False
        #: 下一发子弹最早什么时候能打（`time.monotonic()` 的刻度）。
        #: ★ 这是**唯一**一个时间阈值，值来自 `weapon.ini` 的 `CoolingTime`
        #:   —— 原版这把枪就是这个节奏，不是我拿一台机器观测出来的常量
        #:   （铁律 10 / D29）。
        #: ★★ 它只管**手上这一把**；切走的那几把冻在 `weapon_cd` 里（§126）。
        self.next_fire_at = 0.0
        #: ★★ **切走的那几把枪各自还剩多少冷却（秒）**（§126）。
        #:   原版的三张倒计时表每帧只推「手上那一把」（`0x48bd59` 的键是
        #:   `[持枪器+0x18]`）⇒ 切走就定格、切回来接着走完。
        self.weapon_cd = {}
        #: 同上，**弹匣里还剩几发**也是跟着枪走的。
        self.weapon_rounds = {}
        #: 这一图**每个距离档**的开火日志打过了吗（按状态翻转去重，铁律 10）。
        #: key = `(武器 id, 飞行 tick 取整)` —— 贴脸打一行、远距离再打一行，
        #: 因为「看不见子弹」在两个距离上都出现过，两边的字节都得留证。
        #: ★ 诊断口径，M3b 收口后收回成「本图第一发」。
        self.fire_logged = set()
        #: 这一图命中 / 落空的爆炸日志各打过了吗（同上，诊断口径）。
        self.explode_logged = set()
        #: ★ 这一图**每一类击退结果**的日志打过了吗（同上）。
        #:   key = `(来源, 结果分类)`，例如 `("直接命中", "飞出去")` /
        #:   `("直接命中", "配不上开火记录")` / `("溅射", "撞墙没动")`。
        #:   用户 2026-08-28 报「有时候有击退，有时候没有」——
        #:   这本账就是为了让日志把「哪一类没动、为什么」一次讲清楚。
        self.knock_logged = set()
        #: 这一图的「分裂弹炸成几片」日志打过了吗（同上，§81）。
        self.split_logged = False
        #: ★ 分裂弹撒碎片时的随机数（`0x47c9b7` 那一发 `rand() % n`）。
        #:   做成实例字段是为了**单测能钉住**它 —— 换成 `lambda n: 0` 就
        #:   得到确定的四个角度。默认走 `random.randrange`。
        self.roll = random.randrange
        #: ★ 已经用 `rpChangeWeapon` 向别人**声明过**的武器 id。
        #:   `None` = 还没声明。收方拿它换武器模型 —— 不发的话别人看见的是
        #:   客户端自己给的默认枪，和 bot 打出来的子弹对不上（会话 13 修）。
        #:   换图 / 新一局角色重建，这边跟着清（同 `crouched` 的道理，§41）。
        self.declared_weapon = None
        #: ★★ 闪避（M5-E）：上一次判断过的那一波威胁（`Threat.key` 的元组），
        #:   以及那一波掷出来的「预估失误」动作（`None` = 没失误）。
        #:   判据是**威胁集合变了没有**，不是每一帧重掷（铁律 10 的去重口径）。
        self.dodge_signature = None
        self.dodge_blind = None
        #: 这一帧为了躲子弹要不要蹲（蹲在心跳里是单独一发事件包，§41）。
        self.dodge_crouch = False
        #: ★ **帧序号**：`_tick_bot()` 每走一帧 +1。逐 tick 的决策拿它当
        #:   「同一帧」的判据 —— 挂钟在 Win7 的 3.8 上只有 15.6 ms 粒度。
        self.frame_seq = 0
        #: 这一帧判过没有（缓存键 = `frame_seq`）+ 判出来的结果。
        self.dodge_at = None
        self.dodge_cached = None
        self.dodge_cached_crouch = False
        #: ★★ 打法姿态（M5-C）：`"press"` = 逼近，`"retreat"` = 拉开。
        #:   由双方**血量 + 输出**折算出来的「谁先倒下」决定，见 `_stance()`。
        self.stance = "press"
        #: 拉开时那个后退落脚点 `(x, y)`；`None` = 还没挑 / 已经到了。
        self.retreat_goal = None
        #: ★★ 上一次**真的迈出去**的走位方向（`_walk_to()` 的兜底那一路写）。
        #:   `0` = 还没走过。判「这一次掉头是不是只为了修一个走不满的零头」
        #:   要用它，见 `_walk_to()` 里那段（V0.3 §167）。
        self.walk_last = 0
        #: ★ 诊断（§167）：这一格 `_walk_to()` 拿到的目标点，只给
        #:   `_src()` 那一行用；每格由 `_move_intent()` 先清空。
        self.walk_goal = None
        #: ★★ AI 自己挑出来的那把枪的 ammo id（M5-C）；`None` = 还没挑过，
        #:   按 `weapondata.preferred_for()` 的缺省来。房主锁枪（`weapon_slot`）
        #:   和地上捡来的枪（`item_weapon`）都排在它前面。
        #:   ★ 跟着换图 / 新一局清：新图的距离、地形全变了，上一张图挑的
        #:     那把一个字都不作数。
        self.auto_weapon_id = None
        #: ★ 朝烟雾团乱射时用的那个随机偏移（M5-F）；打出一发之后重掷。
        self.smoke_offset = None
        #: ★★ **这一发**的瞄准失误（`botaim.Miss`），`None` = 还没掷。
        #:   在开火之前掷一次、打出去之后清掉 —— 判据是「开了一枪」这个
        #:   事件，不是时间（铁律 10）。逐帧重掷的话准星会抽搐，而且
        #:   「失误概率」的语义会从「每一发」滑成「每一帧」。
        self.aim_miss = None
        self.aim_miss_rolled = False
        #: 房主用 `/w` 指定的武器**槽位**（1/2/3）；`None` = AI 自主换枪。
        #: ★ **不跟着换图清** —— 那是房主给的房间指令，不是一图之内的
        #: 机器状态；新加入的 bot 在 `_add_one_bot()` 继承 `Room` 那一格。
        self.weapon_slot = None
        #: 房主用 `/hold N` 让它站住了吗。★ 同上，跨图保持。
        #:   站住的时候照常发心跳（真人站着不动也发），只是坐标不再前进 ——
        #:   这样才好测「隔着墙打不打得到」（用户 2026-08-26 要的测试手段）。
        self.holding = False
        #: ★ 诊断开关 `/noboom`：只发 `rpFire`、不发 `rpExplode`
        #:   ⇒ 弹体一直飞不消失、一滴血不掉（§42）。用来分清「看不见子弹」
        #:   到底是**弹体没造出来**还是**爆炸发得太早**。跨图保持。
        #:   ★ M3b 收口后连同 `_cmd_noboom` 一起删。
        self.no_explode = False
        #: ★ 诊断开关 `/slow`：初速降到 1/10，子弹慢慢飞。配合 `/noboom` 用 ——
        #:   「慢 10 倍 + 永不消失」的弹体要是还看不见，那就是**根本没画**，
        #:   而不是「太快没注意到」。跨图保持。M3b 收口后删。
        self.slow_bullet = False
        #: ★★ **这个弹匣里还剩几发**（`weapon.ini` 的 `MagazineCount`）。
        #:   `None` = 还没开过枪 / 这把武器没有弹匣。打空了就停 `ReloadTime`
        #:   再装满 —— **原版就是这么打的**，不停的话持续输出会高出好几倍
        #:   （用户 2026-08-26 报的「1 号武器没有 CD，一会儿就把我秒死了」）。
        #:   换枪 / 换图跟着清。
        self.rounds_left = None
        #: ★★ **体力**（`GameProps.ini` 的 `SpMax` / `SpCharging`）。
        #:   `None` = 还没开打（第一帧补满）。近身冲刺攻击花
        #:   `DashNN-SpCost`，冲刺跑每 tick 花 `FastRunSpCost`，
        #:   平时每 tick 回 `SpCharging`（蹲着 ×2）。
        #:   ⚠ 收方**不替远端角色算体力**，这是 bot 自己给自己上的约束 ——
        #:   不然它可以无限近身，那不是原版的玩法（§64）。
        self.stamina = None
        #: 上一次结算体力的时刻。
        self.stamina_at = 0.0
        #: 正在进行的那一下近身攻击（`DashSwing`）；`None` = 没在冲。
        self.dash_swing = None
        #: 让这个 bot 用近身攻击吗（`/dash` 开关，默认开）。
        #: ★ 留这个开关是因为 `rpDash` 会**吃掉一个弹体句柄**（§64）——
        #:   万一某个角色不是吃 1 个，表现会是「子弹照飞、一滴血不掉」。
        #:   实机上真遇到就 `/dash` 关掉，能当场把这条支路排除掉。
        self.melee = True
        #: ★★ **在飞的子弹**：一串 `Shell`（服务端自己跑的那份弹道，§65）。
        #:   每帧由 `_advance_shells()` 逐 tick 推进，撞到人 / 撞到地形 /
        #:   飞出图外才发 `rpExplode`，炸在**真的撞上的那一点**。
        #:   ⚠ 每一颗都**必须**恰好发一发：句柄记账在开火那一刻就推进了，
        #:   漏发一发，收方那一格计数器就和服务端错开，从此打不掉血（§42）。
        self.pending_shots = []
        #: ★ 地上捡到的特殊武器（`weapondata.Weapon`）；`None` = 用自己那把。
        #:   死了重生时清掉，用完（见下面两格）也清掉。
        self.item_weapon = None
        #: ★★ 捡来那把枪**还能打几发**；`None` = 这把枪不限发数（V0.3 §115）。
        #:   原版的 `ForceCount`，落在武器记录的 `+0x98`、角色那边的
        #:   `[持枪器+0x34]`，每开一发 `0x48baee` 减一。核弹发射器 = 3。
        self.item_weapon_shots = None
        #: ★★ 捡来那把枪**能拿到什么时候**（`time.monotonic()` 的刻度）；
        #:   `None` = 这把枪不限时。原版的 `ForceTime`（`+0x94` -> `[+0x38]`
        #:   = 拿到手的时刻 + 它）。火焰喷射器 15 秒、水炮 10 秒。
        self.item_weapon_until = None
        #: ★★★ **按「还能打几发」算的状态**：`{属性号: 剩余发数}`
        #:   （`gameserver.MAGAZINE_STATUS`，V0.3 §117）。
        #:   强力射击 / 三重射击 / 毒弹这三条在 `Status.ini` 里**只有
        #:   `Magazine`、没有 `Time`**，客户端不会自己撤 —— 得服务端数完
        #:   补一发 `0x040d`。空 = 身上没有这一类状态。
        self.magazine_attrs = {}
        #: ★ 踩到减速胶水之后**减速到什么时候**（`time.monotonic()` 的刻度）；
        #:   `None` = 没中招（V0.3 §101）。
        self.slowed_until = None
        #: ★ 被冰冻**到什么时候**；`None` = 没被冻（V0.3 §106）。
        self.frozen_until = None
        #: ★★ **地上还在烧的火墙**（`FireWall`，§78）。收方只把火画出来，
        #:   算谁被烧的还是「射手那台机器」—— bot 没有本机，所以归这边。
        self.fires = []
        #: ★★★ **每个座位上一次挨火烧是绝对第几个 tick**（§85）。
        #:   原版这一格记在**角色**身上（`[角色+0x160]`，`0x50f7a7`），
        #:   所有火共用一格 ⇒ 一个人 20 个 tick 之内只掉一次火伤，
        #:   叠几道墙也一样。所以这本账不能记在 `FireWall` 上。
        self.burnt = {}
        #: ★★ **进图 / 复活之后的封锁到点时刻**（`time.monotonic()` 的刻度）。
        #:   `None` = 还没开始这一局。原版在 `Character::Respawn`（`0x502fca`）
        #:   里给角色挂了一个 **2000 ms** 的状态 0（`0x5030a0`），而开火输入
        #:   （`0x516471`）和近身输入（`0x515acc`）进门第一件事就是查它 ——
        #:   这就是真人「预备 / 开始那两下没打完不能动手」「刚复活那两秒
        #:   只能跑不能打」的来源（§74）。
        self.act_lock_until = None
        #: ★★ **进图**那一次额外的「连走都不许走」的封锁（`time.monotonic()`）。
        #:   `None` = 还没进图 / 已经过去了。
        #:
        #:   和 `act_lock_until` 的区别是**只在进图那一档挂，复活不挂** ——
        #:   这条分界是用户自己给的两句话（§94）：
        #:   「真人被打死复活后有大概两三秒**只能移动**、不能开枪」
        #:   「一开始我**还不能动**呢，就看见 bot 已经向我这边跑来了」。
        #:   ⇒ 复活那一档拦的只有动手，进图（预备 / 开始）那一档连走位一起拦。
        self.enter_lock_until = None
        #: 上一帧「躺着没有」。拿它做**状态翻转**：躺着 -> 站起来 = 复活了，
        #: 那一刻重新上锁（铁律 10 的口径，不看时间只看事实翻转）。
        self.was_lying = False
        #: ★★ **蓄力开始的时刻**（`PowerControl=2` 的武器，§73）。
        #:   `None` = 手指没按着。真人扔手雷要长按鼠标左键蓄力，
        #:   蓄力值每个逻辑 tick `+2`、封顶 80，松手才扔得出去 ——
        #:   蓄得越久扔得越远。bot 现在也得老老实实按住。
        self.charge_at = None
        # ★ **不调 `register_conn()`**：`_conns` 是「在线的真人」表。
        #   进去的话 `latest_conn()`（控制通道不指定账号时的默认目标）
        #   随时可能变成一个 bot，`tools/gs_ctl.py` 就对着空气发命令了。

    def reset_battle_frame(self):
        """把「这一张图上的帧状态」清干净（换图 / 新一局各调一次）。

        由 `gameserver.reset_sync_trails()` 调 —— 那边同时把真人的位置轨迹
        清掉，两件事必须一起做：轨迹没了，上一张图的落脚点和「消费到第几个
        点」也就都不作数了。

        ★ 「进度条报过了」跟着一起清：本函数正好在**新一轮加载开始**
        那一刻被调（`0x0400` / `0x0417` 广播），不清的话换图那一发会被去重挡掉。

        ★★ 「蹲着没有」也一起清：换图 / 新一局客户端把角色重建，
        `[char+0x2b5]` 跟着归零（`0x4ffc4a`）。这边不清的话两边就对不上了 ——
        bot 以为自己还蹲着，于是**不发**那一发起立的 `rpCrouch`（§41）。

        ★★★ **发弹数也一起清**（M3b）：客户端在「重新加载地形」
        （`ForceReloadTerrain`）时把弹体句柄计数器整个复位成
        `座位 × 100000 + 100002`（`0x47346f`，§42）—— 而本函数被调的那两处
        正是开局和换图，一一对应。这边不清的话，第二张图上 bot 预测的句柄
        比收方大一整局的发弹数，`rpExplode` 全被静默丢弃：**子弹照飞、
        一滴血不掉**，而且一局之内不自愈（D28 的硬约束 2）。
        """
        self.battle_pos = None
        self.battle_pos_prev = None
        # ★ 自己的运动状态跟着清：新一张图的地形、出生点全变了，
        #   上一张图的落脚点和速度一个字都不作数（同 `battle_pos`）。
        self.body = None
        self.on_ground = None
        # ★ 待定的重生点跟着清：新一张图的出生点表整个换了一份（§91）。
        self.pending_spawn = None
        self.intent = None
        self.intent_tick = None
        self.aim = None
        self.nav_path = []
        self.nav_goal = None
        self.nav_started = False
        self.nav_failed = None
        botplan.forget(self)
        self.nav_planned_at = None
        self.nav_double_jump = False
        self.path_breakable = None
        self.path_breakable_prefix = []
        self.path_breakable_only = False
        # ★ 牵引绳的记账跟着清：新一张图上「落后多少」要从头量（同 `body`）。
        self.leash_lagging = False
        self.leash_mark = None
        self.leash_gap = 0.0
        self.leash_anchor = None
        self.move_down = False
        self.down_latch = False
        self.beat_pending = False
        self.trail_mark = None
        self.trail_heading = 0
        self.load_progress = None
        self.load_progress_confirmed = frozenset()
        self.crouched = False
        self.sync.reset_projectiles()
        # ★ 在飞的子弹一起丢：收方的弹体表和句柄计数器这一刻也整个复位
        #   （`ForceReloadTerrain`），上一张图的弹体在那边已经不存在了 ——
        #   补发只会拿一个查不到的句柄去撞 `0x492750` 那个静默丢弃。
        self.pending_shots = []
        # ★ 捡来的枪跟着清：新一局 / 换图之后地上那件东西已经不存在了。
        self.drop_item_weapon()
        # ★ 按发数算的状态跟着清：客户端重建角色时属性表也整个没了。
        self.magazine_attrs = {}
        self.slowed_until = None
        self.frozen_until = None
        # ★ 火墙跟着清：收方的弹体表这一刻整个复位，上一张图那几团火
        #   在那边已经不存在了（同 `pending_shots`）。
        self.fires = []
        # ★ 火烧的免伤时刻戳跟着清：换图 / 新一局客户端把角色重建，
        #   `[角色+0x160]` 也跟着归零（同 `crouched` 的道理，§85）。
        self.burnt = {}
        self.next_fire_at = 0.0
        self.rounds_left = None
        # ★ 换图 / 新一局：客户端把角色和持枪器一起重建，三张倒计时表清空。
        self.weapon_cd = {}
        self.weapon_rounds = {}
        self.fire_logged = set()
        self.explode_logged = set()
        self.knock_logged = set()
        self.split_logged = False
        # ★ 体力和近身动作跟着一起清：换图 / 新一局客户端把角色重建，
        #   `[char+0x2b5]` 那一套状态全归零（同 `crouched` 的道理，§41），
        #   而正在进行的那一下的句柄在收方那边已经不存在了。
        self.stamina = None
        self.stamina_at = 0.0
        self.dash_swing = None
        # ★★ 封锁跟着清：新一局 / 换图之后客户端把角色重新放进图里，
        #   那一刻 `Character::Respawn` 又挂一次 2000 ms 的状态 0（§74）。
        #   清成 `None` = 「还没上过锁」，`_tick_bot` 的第一帧会补上。
        self.act_lock_until = None
        self.enter_lock_until = None
        self.was_lying = False
        # ★ 手指也松开：换图之后武器要重新声明，蓄力从零开始（§73）。
        self.charge_at = None
        # ★ 换图 / 新一局客户端把角色重建，武器回到它自己的默认那把 ——
        #   这边不清的话 bot 以为「已经声明过了」，于是**不再发**
        #   `rpChangeWeapon`，别人看到的枪和它打出来的子弹从此对不上。
        #   和 `crouched` 是同一个坑（§41）。
        self.declared_weapon = None
        # ★ AI 挑的那把跟着清（同 `declared_weapon`）：新图的交战距离和
        #   地形全变了，重新挑一次。
        self.auto_weapon_id = None
        self.stance = "press"
        self.retreat_goal = None
        self.walk_last = 0
        self.walk_goal = None
        self.dodge_signature = None
        self.dodge_blind = None
        self.dodge_crouch = False
        self.dodge_at = None
        self.dodge_cached = None
        self.dodge_cached_crouch = False
        self.aim_miss = None
        self.aim_miss_rolled = False
        self.smoke_offset = None

    @property
    def weapon(self):
        """这个 bot 用哪把枪（`weapondata.Weapon`），`None` = **不开火**。

        ★ 做成 property 是为了跟着 `/c` 和 `/w` 走：换角色 / 换枪时
        命令层只改 `character_id` / `weapon_slot`，武器自动跟着变，
        不用记得同步第二个字段。表本身有缓存，每帧问一次的开销可以忽略。

        `None` 的情况是「这个角色没有一把**句柄步进确定的直射武器**」
        （`weapondata._is_usable` 的五个条件，D29）。这时 bot 只跑不打 ——
        **别随便挑一把凑合的**：步进猜错的表现是「子弹飞过去不炸」，
        而且静默、一局之内不自愈（§42）。

        ★ 房主用 `/w` 指定过槽位就**只能**用那一把；那一把不可用就返回
        `None`（不开火），绝不偷偷退回别的枪。用户要求的语义是“锁定武器”，
        自动回退会让 `/w` 表面成功、实际又自行换枪。
        """
        if self.weapon_slot is not None:
            return weapondata.slot_for(self.character_id, self.weapon_slot)
        # ★ 只有**自动换枪模式**才允许地上捡到的特殊武器压过普通武器
        # （§223 / §100）。`/w 1..3` 的“只能用指定武器”也包含这条支路。
        if self.item_weapon is not None:
            return self.item_weapon
        # ★★ AI 自己挑的那把（M5-C）。挑不出来（还没打过照面 / 表里没有）
        #    才退回 `weapondata` 的缺省首选。
        if self.auto_weapon_id is not None:
            picked = weapondata.get(self.auto_weapon_id)
            if picked is not None:
                return picked
        return weapondata.preferred_for(self.character_id)

    def drop_item_weapon(self):
        """把捡来的那把枪连同它的两个计数一起丢掉。

        ★ 三格要一起动，所以收在一个地方：漏清计数的话，下一次捡到同一类
        枪时会带着上一把的余额（原版 `0x48be2f` 也是三格一起清）。
        ★ **只改状态、不发包** —— `rpChangeWeapon` 由调用方决定发不发
        （重生 / 换图那两条路上客户端自己就把武器复位了）。
        """
        self.item_weapon = None
        self.item_weapon_shots = None
        self.item_weapon_until = None

    def __repr__(self):
        return f"<BotConn {self.nickname} 座位 {self.my_seat}>"

    def is_bot_conn(self):
        """★ `gameserver` 认 bot 的**唯一**入口（它 import 不了本模块，§14）。"""
        return True

    def area_effect_origin(self):
        """「作用于周围」的道具（冰冻 / 烟雾）以 bot 此刻的落脚点为中心。

        父类那一版读的是 `sync_trail` —— bot 没有上行心跳，那条恒空。
        """
        return None if self.battle_pos is None else tuple(self.battle_pos)

    # -- 发送：全部空操作 ---------------------------------------------------
    def send(self, plain):
        """空操作 —— 只留下**换代模型**那一步。

        ★ 返回而不是抛异常：`battle_broadcast()` / `broadcast()` 会对房里
        每一个成员调它，抛异常等于让 bot 的存在弄坏真人的广播。

        ★★ **但 `note_epoch_from_frame()` 必须照跑**（V0.3 §26）：
        局号是收包队列的**纪元号**，`0x0400` / `0x0403` 每发出一次，房里
        每个人的号就 +1。真人那一份是在 `Conn.send()` 里跟着字节走的
        —— bot 收到的是**同一串字节**，所以在同一个地方跟一格，两边永远同代。

        不跟的话：真人进了「战斗代」，bot 还停在「房间代」，
        `relayserver.deliver()` 判成**跨代**，bot 的同步包一发都投不出去
        —— 症状是「bot 在别人屏幕上一动不动」，和「包压根没合成出来」
        长得一模一样。第二局尤其明显（第一局还能靠 `_epoch_of` 的补锚兜住）。
        """
        self.note_epoch_from_frame(plain)

    @contextlib.contextmanager
    def send_batch(self, reason=""):
        """空操作版的批量发送。`with` 语义要保住，否则调用方会炸。"""
        yield

    def close_now(self):
        return

    def kill_stream(self, why):
        return

    # -- 日志 ---------------------------------------------------------------
    def log(self, msg):
        # ★★ 异步（用户 2026-09-01）：这个函数几乎总是在 `room.sim_lock` 里面被
        #    调到（`_tick_bot` 整格持锁），同步写盘就是整个房间跟着等磁盘，
        #    而真人的 `forward_peer_data` 又在等同一把锁。见 `asynclog.py` 开头。
        asynclog.emit(f"[{gameserver.ts()}] {self.nickname} {msg}")

    def online(self, msg):
        """bot 的上下线不进运营事件日志 —— `online.log` 记的是真人的流水。"""
        return

    def online_debug(self, msg):
        return

    def peer(self):
        return "bot"

    # -- 座位 ---------------------------------------------------------------
    def my_nickname(self):
        return self.nickname

    def seat_snapshot(self):
        """给 `Lobby.add_bot()` 用的座位。真人那一版是按存档做的。"""
        return Seat(self,
                    username="",
                    nickname=self.nickname,
                    level=BOT_LEVEL,
                    character_id=self.character_id,
                    is_bot=True)

    def refresh_seat(self):
        """★ **必须是空操作。**

        `Conn.refresh_seat()` 会拿 `display_name(self.account)` 去刷座位昵称，
        而 bot 的 `account` 是 `None` —— 照跑一遍就把「bot 1」刷成空串了。
        它的调用方 `send_session_members()` 是**每个房间成员**都会被调到的
        （房主换人时 `after_someone_left` 就会调），所以这一条不是理论风险。
        """
        room = self.lobby_room()
        return None if room is None else room.seat_of(self)


# ----------------------------------------------------------------------------
# 命令
# ----------------------------------------------------------------------------
#: `/help` 打出来的那张表。
#:
#: ★ **总共只能有三行**（§20）：房间里的聊天框一次只看得见 4 行，多发的会被
#: 顶出去 —— 原来那版 8 行，玩家永远只看得到后 4 行。所以一条命令一行的排法
#: 不能要，改成**多条命令挤一行**、命令之间用 `;` + 两个空格分隔。
#:
#: ★ 每行**控制在 50 个半角宽以内**：聊天框到边就自己折行，而折出来的行同样
#: 占那 4 行的额度。50 是实测过的安全值 —— 旧版最长那行（`/c` 那条）
#: 54 宽在框里没折，往回收一点留余量。改这张表时请用同样的口径数宽度
#: （中文和全角标点算 2，ASCII 算 1）。
HELP_LINES = (
    "bot 命令（房主专用，N = 座位号，/? 重看）：",
    "/a [n] 加 n 个（默认 1）;  /r 全部准备（再敲取消）",
    "/c N M 换角色（M=1~3）;  /t N 换队（组队战）",
    "/s 简单; /m 中等; /h 困难; /w 0 自动换枪",
)

#: ★ **战斗中**的 `/?`。房间里那几条会被 `MUTATING_COMMANDS` 挡掉，列出来
#: 只会占满那 4 行的额度（§20），所以这里只放战斗中真能用的两条。
BATTLE_HELP_LINES = (
    "AI：/s 简单; /m 中等; /h 困难; /w 0 自动",
    "/w M 锁武器; /hold N 站住; /dash 近身开关",
    "查子弹: /noboom 只飞不炸;  /slow 降到 1/10 速",
)


def parse_command(text):
    """把一行聊天解析成 `(命令名, [参数...])`；不是 bot 命令就返回 ``None``。

    命令名统一小写、不带 `/`。参数原样保留（数字由各命令自己解析并报错）。
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped.startswith(COMMAND_PREFIX):
        return None
    parts = stripped[len(COMMAND_PREFIX):].split()
    if not parts:
        return None
    return parts[0].lower(), parts[1:]


#: 命令名 -> 处理函数。`/?` 是 `/help` 的短别名；`/h` 已按用户要求改成
#: 困难难度，不能再同时当帮助。
#: 在这张表里 = 「这是一条 bot 命令」，命令层据此决定要不要吞掉这行聊天。
#:
#: ★ `team` 还在表里，但它**不是**换队命令，只是一行「请改用 /t」的提示
#: —— 客户端自己把 `/team ` 当队伍聊天吃掉了，服务端根本收不到（§19）。
COMMAND_NAMES = ("a", "c", "t", "team", "r", "s", "m", "h", "w",
                 "hold", "help", "?")


def _seat_arg(args, index=0):
    """取第 `index` 个参数当座位号。返回 `(座位号, 错误文案)`。"""
    if len(args) <= index:
        return None, "少了座位号。"
    try:
        seat = int(args[index])
    except ValueError:
        return None, f"座位号 {args[index]!r} 不是数字。"
    if not 0 <= seat < ROOM_SEAT_COUNT:
        return None, f"座位号 {seat} 越界，只有 0~{ROOM_SEAT_COUNT - 1}。"
    return seat, None


def _bot_seat(room, seat_index):
    """取 `seat_index` 上的 bot 座位。返回 `(Seat, 错误文案)`。"""
    seat = room.seats[seat_index]
    if seat is None:
        return None, f"{seat_index} 号位是空的。"
    if not seat.is_bot:
        return None, f"{seat_index} 号位坐的是 {seat.nickname}，不是 bot。"
    return seat, None


def _team_balance_warning(room):
    """组队房两队人数不等时的一行警告；平衡就返回 ``None``。

    客户端 `0x468495` 数 1 队和 2 队的人数，不等就直接拒绝开局并弹
    「两组人数不相同。请调整人数。」（V0.3 §8）—— 所以这一条必须当场说，
    不能等房主按了开始才发现。
    """
    if room.team_layout() != TEAM_LAYOUT_TEAMS:
        return None
    a = sum(1 for s in room.seats if s is not None and s.team == TEAM_A)
    b = sum(1 for s in room.seats if s is not None and s.team == TEAM_B)
    if a == b:
        return None
    return f"⚠ 两队人数不等（{a} : {b}），客户端不会让开局，请再调整一下。"


def _align_epoch(machine, room):
    """把新 bot 的换代模型对齐到房间当前这一代（D138 对 bot 的等价物）。

    真人是靠进房那一发 `0x0303 gspSession` 的包尾 u16 对上的
    —— 那是原版留给服务端的唯一一个「说几就是几」的入口（`0x556ed1`）。
    bot 收不到任何包，所以这里直接把模型设成同一个数。

    ★ 不对齐会怎样：局号停在 -1，而房里真人已经是 `room.epoch_value`。
    同一代里编号不同 `deliver()` 会按收件人重新盖章，功能上还能转发，
    但每一对收发都会刷一行「同代改写局号 …… ★ 开局前的强制对齐正常时
    不该出现这一行」的警告 —— 把一条真的诊断信道用成噪声，比多写三行贵。
    """
    relayserver.epoch_state(machine).assign(
        room.epoch_value, gameserver.room_generation(room))


def _add_one_bot(conn, room):
    """加一个 bot，返回 `(座位号, 错误文案)`。`/a` 的单步。"""
    index = room.free_seat()
    if index is None:
        return None, "房间已经满了（6 个座位都有人），先把 bot 踢掉一个再加。"
    machine = BotConn(index)
    # ★ 房间级 `/w` 要覆盖**后来加入**的 bot。0 = 自动，因此机器态用 None；
    # 1..3 = 锁定。房间跨游戏局保留、销毁房间后重建默认 0（Room.__init__）。
    room_slot = int(getattr(room, "bot_weapon_slot", 0) or 0)
    machine.weapon_slot = room_slot or None
    seat = machine.seat_snapshot()
    index = gameserver.LOBBY.add_bot(room, seat)
    if index is None:                      # 拿锁那一刻被别人坐满了
        return None, "房间刚好被坐满了，没加上。"
    _align_epoch(machine, room)
    conn.log(f"   /a: 座位 {index} 加入 {seat.nickname}"
             f"（角色 {seat.character_id} 队伍 {seat.team}）")
    conn.online(f"房间 + bot 房间 #{room.room_id} 座位={index} "
                f"房主={conn.account_name!r}")
    # ★ action 0 是唯一会**建**座位 3D 模型的分支（`0x405e1c`），换角色那个
    #   action 4 建不出来。`broadcast_seat_slot` 发给房里每一个人**含房主
    #   自己** —— 房主的客户端和别人一样，只认这一发。
    conn.broadcast_seat_slot(room, index, gameserver.SEAT_ACTION_JOIN,
                             reason=f"：座位 {index} 加入 bot")
    conn.room_system_chat(f"{seat.nickname} 加入了房间。")
    return index, None


def _cmd_add(conn, room, args):
    """`/a [n]` —— 一次加 `n` 个 bot（不给就是 1），坐最小的空座。

    ★ 没有对应的删除命令：**客户端自带踢人功能**，bot 座位和真人座位在
    它眼里是一样的（用户 2026-08-28 拍板，D56）。以前那条 `/del N` 是
    在踢人这条路验通之前的替代品，现在只是多一个要记的命令。
    """
    count = 1
    if args:
        try:
            count = int(str(args[0]).strip())
        except (TypeError, ValueError):
            return f"/a 用法：/a [个数]。{args[0]!r} 不是数字。"
        if count < 1:
            return f"/a 用法：/a [个数]。{count} 至少要是 1。"
        # ★ 上界不是我们定的阈值：房间就 6 个座位（`ROOM_SEAT_COUNT`），
        #   写多了下面那个循环自己会撞到「房间满了」。
        count = min(count, ROOM_SEAT_COUNT)
    added = []
    error = None
    for _ in range(count):
        index, error = _add_one_bot(conn, room)
        if index is None:
            break
        added.append(index)
    if not added:
        return error
    if error:
        # 加了几个之后满了 —— 把加成功的报出去，再说为什么停下。
        return f"加了 {len(added)} 个（座位 {added}），{error}"
    return _team_balance_warning(room)


def _cmd_char(conn, room, args):
    """`/c N M` —— 换 bot 的角色。M 是面板序号 **1~3**（D6 / D54）。

    ★ 商城角色（面板 4~14）**不给 bot 用**：它们的 2/3 号武器里有反弹弹、
    炮台、等离子炮这些服务端还没有飞行模型的类（§72），逐个适配代价太大
    （用户 2026-08-27 拍板）。三个基础角色的 9 把武器全部可用。
    """
    span = len(BOT_CHARACTER_PANEL_IDS)
    index, error = _seat_arg(args)
    if error:
        return f"/c 用法：/c <座位号> <角色序号 1~{span}>。{error}"
    if len(args) < 2:
        return f"/c 用法：/c <座位号> <角色序号 1~{span}>。少了角色序号。"
    character = character_for_panel(args[1])
    if character is None:
        return (f"角色序号 {args[1]!r} 不对，只能是 1~{span}"
                f"（bot 只能用初期这三个角色，商城角色不给 bot 用）。")
    seat, error = _bot_seat(room, index)
    if error:
        return f"换不了角色：{error}"
    seat.update(character_id=character)
    if isinstance(seat.conn, BotConn):
        seat.conn.character_id = character
    conn.log(f"   /c: 座位 {index} 的 {seat.nickname} -> "
             f"面板 {args[1]} = 角色 id {character}")
    # ★ 用 action 3（按座位数据重建模型）而不是 action 4：后者会让客户端播
    #   一句韩文「%s님이 %s 캐릭터로 선택되었습니다.」（`0x406520`）。
    #   我们自己用中文说一遍就够了。
    conn.broadcast_seat_slot(room, index, gameserver.SEAT_ACTION_RESYNC,
                             reason=f"：座位 {index} 的 bot 换角色")
    conn.room_system_chat(f"{seat.nickname} 换成了 {args[1]} 号角色。")
    return None


def _cmd_team(conn, room, args):
    """`/t N` —— bot 换队（1↔2）。只有组队战有效。

    ★ **命令名不能叫 `/team`**：客户端把 `/team ` 当成队伍聊天的前缀自己吃掉了
    （见 `CLIENT_RESERVED_PREFIXES` / §19），服务端一个字都收不到。
    ★ `/t ` 不在那张前缀表里（`/tell ` / `/to ` 都比它长，第 3 个字符就分岔），
    所以这个短名是安全的。
    """
    index, error = _seat_arg(args)
    if error:
        return f"/t 用法：/t <座位号>。{error}"
    layout = room.team_layout()
    if layout != TEAM_LAYOUT_TEAMS:
        # ★ 「个人战不能分队」不是偷懒：客户端的队伍记录数组只有两格，
        #   个人战必须全是 0，闯关必须全在 1 队，改了会越界写进别人的战绩
        #   （lobby.py 开头 `TEAM_LAYOUT_*` 的推演，V0.3 §8）。
        why = ("这是闯关房，大家都是队友" if layout == lobby_module.TEAM_LAYOUT_COOP
               else "这是个人战房，本来就不分队")
        return f"换不了队：{why}。要分队请在房间设置里改成组队战。"
    seat, error = _bot_seat(room, index)
    if error:
        return f"换不了队：{error}"
    want = TEAM_B if int(seat.team) == TEAM_A else TEAM_A
    seat.update(team=want)
    conn.log(f"   /t: 座位 {index} 的 {seat.nickname} -> {want} 队")
    conn.broadcast_seat_slot(room, index, gameserver.SEAT_ACTION_RESYNC,
                             reason=f"：座位 {index} 的 bot 换队")
    conn.room_system_chat(f"{seat.nickname} 换到了 {want} 队。")
    return _team_balance_warning(room)


def _cmd_ready(conn, room, args):
    """`/r` —— 所有 bot 一键准备；**已经准备好的话再敲一次就全部取消**。

    ★ 开关口径和真人按「游戏准备」一致（`on_toggle_ready` 也是翻转），
    判据是**当前状态**而不是敲了第几次（铁律 10）：房里的 bot 只要还有一个
    没准备好，这一发就是「全部准备」；全都准备好了才是「全部取消」。
    """
    seats = room.bot_seats()
    if not seats:
        return "房间里一个 bot 都没有，先 /a 加一个。"
    present = [room.seats[i] for i in seats if room.seats[i] is not None]
    want = not all(seat.ready for seat in present)
    changed = []
    for index in seats:
        seat = room.seats[index]
        if seat is None or bool(seat.ready) == want:
            continue
        seat.update(ready=want)
        changed.append(index)
        # ★ 和真人按「游戏准备」走同一条路（`on_toggle_ready`）：action 3。
        #   房里其他人的「准备中」标记、房主能不能按开始，全靠这一发。
        conn.broadcast_seat_slot(room, index, gameserver.SEAT_ACTION_RESYNC,
                                 reason=f"：座位 {index} 的 bot "
                                        f"{'准备' if want else '取消准备'}")
    if not changed:
        return f"{len(seats)} 个 bot 的准备状态没变化。"
    conn.log(f"   /r: 座位 {changed} 的 bot "
             f"{'已准备' if want else '已取消准备'}")
    conn.room_system_chat(
        f"{len(changed)} 个 bot {'准备好了' if want else '取消了准备'}。")
    return _team_balance_warning(room) if want else None


def _cmd_team_alias(conn, room, args):
    """`/team` —— 只回一行「请改用 /t」。

    ★ 带参数的 `/team 1` **永远走不到这里**：客户端匹配的是 `"/team "`
    （连空格），中了就切掉前缀当队伍聊天发出去（§19）。不带参数的光杆
    `/team` 差那个空格，反而能原样送到服务端 —— 这一条就是为它准备的，
    玩家敲错时至少能看见该敲什么。
    """
    return "换队请敲 /t N（例：/t 1）—— /team 被客户端当成队伍聊天吃掉了。"


def _battle_bots(room, args):
    """战斗命令的座位解析：给了 N 就只作用于那一个，没给就是**全部** bot。

    返回 `(座位号列表, 错误提示)`。房里通常只有一个 bot，不带参数更顺手。
    """
    if not args:
        seats = room.bot_seats()
        return (seats, None) if seats else ([], "房里没有 bot。")
    index, error = _seat_arg(args)
    if error:
        return [], error
    seat, error = _bot_seat(room, index)
    return ([], error) if error else ([index], None)


def _set_difficulty(conn, room, args, level):
    """把全房间 bot 难度切到 `level`；三条短命令共用。"""
    command = {"easy": "/s", "medium": "/m", "hard": "/h"}[level]
    if args:
        extra = "（站桩诊断已迁到 /hold [座位号]）" if level == "easy" else ""
        return f"{command} 不带参数。{extra}"
    room.bot_difficulty = level
    profile = difficulty_profile(room)
    label = BOT_DIFFICULTY_LABELS[level]
    conn.log(f"   {command}: bot 难度 -> {level} "
             f"瞄准失误={profile['aim_error']:.0%} "
             f"闪避失误={profile['dodge_error']:.0%}")
    conn.room_system_chat(
        f"全体 bot 已调为{label}难度（瞄准失误 {profile['aim_error']:.0%}，"
        f"闪避失误 {profile['dodge_error']:.0%}）。")
    return None


def _cmd_easy(conn, room, args):
    """`/s` —— 全房间 bot 切到简单（笨）难度。"""
    return _set_difficulty(conn, room, args, "easy")


def _cmd_medium(conn, room, args):
    """`/m` —— 全房间 bot 切到中等难度（新房间默认）。"""
    return _set_difficulty(conn, room, args, "medium")


def _cmd_hard(conn, room, args):
    """`/h` —— 全房间 bot 切到困难（聪明）难度。"""
    return _set_difficulty(conn, room, args, "hard")


def _cmd_hold(conn, room, args):
    """`/hold [N]` —— 让 bot **站在原地不动**（再敲一次恢复）。

    ★ 这是**测试手段**，不是玩法：bot 平时走的是真人趟过的路（D16），
    所以「隔着墙打不打得到」这种事在实机上根本碰不上 —— 中间不可能有墙。
    站住之后房主可以自己走开、绕到地形后面，才验得了 `line_blocked()`
    那条判据（用户 2026-08-26 要的）。

    ★ 战斗中**必须能用**（那正是要用它的时候），所以它不在
    `MUTATING_COMMANDS` 里。
    """
    seats, error = _battle_bots(room, args)
    if error:
        return f"/hold 用法：/hold [座位号]（不给就是全部）。{error}"
    changed = []
    for index in seats:
        machine = room.seats[index].conn
        if not isinstance(machine, BotConn):
            continue
        machine.holding = not machine.holding
        changed.append((index, machine.holding))
    if not changed:
        return "没有可以操作的 bot。"
    held = [str(i) for i, on in changed if on]
    freed = [str(i) for i, on in changed if not on]
    parts = []
    if held:
        parts.append(f"座位 {'、'.join(held)} 的 bot 站住不动了")
    if freed:
        parts.append(f"座位 {'、'.join(freed)} 的 bot 恢复跟随")
    conn.log(f"   /hold: {changed}")
    conn.room_system_chat("；".join(parts) + "。")
    return None


def _cmd_dash(conn, room, args):
    """`/dash [N]` —— 让 bot **用不用近身冲刺攻击**（默认用，再敲一次关掉）。

    真人双击左右方向键、消耗体力打出的那一下（§64）。bot 够得着就冲。

    ★ 为什么留这个开关：`rpDash` 在收方会创建一个 `DashDamage` 对象，
    **和弹体共用同一个句柄计数器**（`0x502229`）。语料量出来是「一发吃 1 个」，
    但万一某个角色不是，表现会是**子弹照飞、一滴血不掉**（§42 那个静默丢弃）。
    实机上真碰到，`/dash` 关掉就能当场把这条支路排除掉。

    ★ 战斗中必须能用，所以它不在 `MUTATING_COMMANDS` 里。
    """
    seats, error = _battle_bots(room, args)
    if error:
        return f"/dash 用法：/dash [座位号]（不给就是全部）。{error}"
    changed = []
    for index in seats:
        machine = room.seats[index].conn
        if not isinstance(machine, BotConn):
            continue
        machine.melee = not machine.melee
        if not machine.melee:
            machine.dash_swing = None
        changed.append((index, machine.melee))
    if not changed:
        return "没有可以操作的 bot。"
    on = [str(i) for i, flag in changed if flag]
    off = [str(i) for i, flag in changed if not flag]
    parts = []
    if on:
        parts.append(f"座位 {'、'.join(on)} 的 bot 会用近身攻击了")
    if off:
        parts.append(f"座位 {'、'.join(off)} 的 bot 不再近身")
    conn.log(f"   /dash: {changed}")
    conn.room_system_chat("；".join(parts) + "。")
    return None


def _cmd_noboom(conn, room, args):
    """`/noboom [N]` —— 让 bot **只发 `rpFire`、不发 `rpExplode`**（再敲一次恢复）。

    ★★ **这是一件诊断工具，不是玩法**（M3b 收口后删）。用户 2026-08-26 报
    「看不见子弹」，而「弹体到底有没有被造出来」在服务端这边看不出来 ——
    句柄是**收方自己**按顺序分配的，服务端只是预测。

    §42 查实：**不发 `rpExplode` 的话弹体不会消失，一直飞下去**。所以：

    * 开着 `/noboom` 能看见子弹飞（而且飞出屏幕也不消失、一滴血不掉）
      ⇒ 弹体造得出来、也会动 ⇒ 问题在**爆炸发得太早**（时序）；
    * 开着也**什么都看不见** ⇒ 弹体压根没造出来或者不动
      ⇒ 问题在 `rpFire` 的内容，得回去逐字段对。

    ★ **不影响句柄记账**：句柄是开火那一刻分配的（§43），少发爆炸只是少一次
    「让它消失」，收方那一格计数器照样对得上 —— 关掉开关就能接着打死人。
    """
    seats, error = _battle_bots(room, args)
    if error:
        return f"/noboom 用法：/noboom [座位号]（不给就是全部）。{error}"
    changed = []
    for index in seats:
        machine = room.seats[index].conn
        if not isinstance(machine, BotConn):
            continue
        machine.no_explode = not machine.no_explode
        changed.append((index, machine.no_explode))
    if not changed:
        return "没有可以操作的 bot。"
    on = [str(i) for i, flag in changed if flag]
    off = [str(i) for i, flag in changed if not flag]
    parts = []
    if on:
        parts.append(f"座位 {'、'.join(on)} 的子弹只飞不炸（也不掉血）")
    if off:
        parts.append(f"座位 {'、'.join(off)} 恢复正常爆炸")
    conn.log(f"   /noboom: {changed}")
    conn.room_system_chat("；".join(parts) + "。")
    return None


def _cmd_slow(conn, room, args):
    """`/slow [N]` —— 把 bot 的子弹初速降到 1/10（再敲一次恢复）。诊断用。

    ★★ 它和 `/noboom` 配合起来回答一个问题：「看不见子弹」到底是
    **弹体没被画出来**，还是**画了但太快没注意到**。
    慢 10 倍 + 永不消失的弹体要是还看不见，那就是前者，铁证。

    ⚠ 抛物线武器的落点会偏（角度没重解）—— 这个开关只用来看轨迹。
    ★ M3b 收口后连同 `_slow_shot` 一起删。
    """
    seats, error = _battle_bots(room, args)
    if error:
        return f"/slow 用法：/slow [座位号]（不给就是全部）。{error}"
    changed = []
    for index in seats:
        machine = room.seats[index].conn
        if not isinstance(machine, BotConn):
            continue
        machine.slow_bullet = not machine.slow_bullet
        changed.append((index, machine.slow_bullet))
    if not changed:
        return "没有可以操作的 bot。"
    on = [str(i) for i, flag in changed if flag]
    off = [str(i) for i, flag in changed if not flag]
    parts = []
    if on:
        parts.append(f"座位 {'、'.join(on)} 的子弹降到 1/10 速")
    if off:
        parts.append(f"座位 {'、'.join(off)} 恢复原速")
    conn.log(f"   /slow: {changed}")
    conn.room_system_chat("；".join(parts) + "。")
    return None


def _apply_gun(conn, room, index, slot):
    """把一个 bot 切到 `slot`（0=自动，1..3=锁定）。

    返回 `(通告文案, 错误文案)`；这里只改这一个 bot，房间默认值由
    `_cmd_gun()` 的单参数分支维护。
    """
    seat, error = _bot_seat(room, index)
    if error:
        return None, error
    machine = seat.conn
    if not isinstance(machine, BotConn):
        return None, f"{index} 号位上不是 bot。"
    if slot == 0:
        machine.weapon_slot = None
        chosen = machine.weapon
        if room.is_playing() and chosen is not None:
            with contextlib.suppress(botsync.SyncInvariantError):
                _declare_weapon(machine, index, chosen)
        detail = ("当前没有可用武器" if chosen is None
                  else f"当前选择 {chosen.raw['slot']} 号（{chosen.damage} 点伤害）")
        conn.log(f"   /w: 座位 {index} 的 {seat.nickname} -> 自动换枪；{detail}")
        return f"{seat.nickname} 恢复自动换枪（{detail}）。", None
    if slot not in (1, 2, 3):
        return None, f"武器槽 {slot} 不对，只能是 0~3（0 = 自动换枪）。"
    choices = weapondata.usable_for(machine.character_id)
    if not choices:
        return None, f"{seat.nickname} 这个角色一把能用的武器都没有，它只跑不打。"
    chosen = weapondata.slot_for(machine.character_id, slot)
    if chosen is None:
        ok = "、".join(str(w.raw["slot"]) for w in choices)
        return None, f"{seat.nickname} 没有能用的 {slot} 号武器槽。可选：{ok}。"
    machine.weapon_slot = slot
    # ★ **当场把新枪声明出去**（用户 2026-08-27 报的）：不发的话别人手里那把
    #   枪要等到 bot **下一次开火**才变（`_declare_weapon` 原来只挂在
    #   `_try_fire` 上）—— 房主敲完命令盯着看，模型半天不动，
    #   过一会儿突然跳变。换枪是**这条命令**造成的事实，就该在这儿报出去。
    #   ★ 只在战斗中发得出去（房间里 bot 还没有同步流）。
    if room.is_playing():
        with contextlib.suppress(botsync.SyncInvariantError):
            _declare_weapon(machine, index, machine.weapon)
    conn.log(f"   /w: 座位 {index} 的 {seat.nickname} -> 槽位 {slot} "
             f"= {chosen.id}({chosen.section}) 伤害 {chosen.damage} "
             f"步进 {chosen.handle_step} 间隔 {chosen.fire_interval_ms}ms")
    return (f"{seat.nickname} 换成了 {slot} 号武器（{chosen.damage} 点伤害）。",
            None)


def _cmd_gun(conn, room, args):
    """`/w [N] [M]` —— 自动/锁定 bot 武器。四种叫法：

        /w        列出每个 bot 有哪些武器槽可用
        /w 0      ★ 全部 bot 恢复 AI 自主换枪
        /w M      ★ 全部 bot 锁定 M 号武器（M = 1/2/3）
        /w N M    只改 N 号位；M 也可为 0

    单参数形式同时写进 `Room.bot_weapon_slot`：跨游戏局/换图保留，后来加入的
    bot 自动继承；新建房间由 `Room.__init__` 恢复 0。两参数形式保留为诊断
    入口，只改指定 bot，不改变房间给后来 bot 的默认值。

    ★★ **一个参数为什么当武器槽、不当座位号**：房里通常只有一个 bot，
    「给所有 bot 换枪」是最常敲的那一条；而「我想知道 N 号位有哪些枪」不带
    参数一次全列出来更省事（聊天框一次只看得见 4 行，§20，所以列表本来就
    得挤成一行）。⚠ 座位号和武器槽的取值范围是重叠的（0~5 vs 1~3），
    两种叫法不可能靠数值区分 —— 这里是**按参数个数**分流，不是按数值猜。

    `weapon_slot is None` 才允许 AI / 道具换枪；1..3 时 `BotConn.weapon`
    只返回指定槽，连地上捡到的临时枪也不能抢走它。

    ★ 只列 / 只接受 `weapondata` 认可的武器（句柄步进算得出 + 有伤害 +
    初速模式认得 + 弹体类服务端有飞行模型）：步进猜错的表现是「子弹飞过去
    不炸」，而且静默、一局之内不自愈（§42）。

    ★ 会话 21（§72）之后 16 个玩家角色里**10 个三个槽位全可用**；剩下
    6 个各缺一个槽：106 / 110 的 2 号是**反弹弹**（会弹墙）、107 / 108 的
    2 号是**炮台**、109 的 3 号是**等离子炮**（那几类的飞行服务端还没有
    模型），角色 3 的 3 号是图腾发射器（`Damage=0`，打不动人）。
    """
    # 不带参数：把每个 bot 有哪些槽列出来（挤成一行，§20）。
    if not args:
        seats = room.bot_seats()
        if not seats:
            mode = int(getattr(room, "bot_weapon_slot", 0) or 0)
            return ("房里没有 bot。当前房间武器模式："
                    + ("自动换枪。" if mode == 0 else f"锁定 {mode} 号武器。"))
        lines = []
        for index in seats:
            machine = room.seats[index].conn
            if not isinstance(machine, BotConn):
                continue
            choices = weapondata.usable_for(machine.character_id)
            if not choices:
                lines.append(f"{index}: 这个角色一把能用的武器都没有")
                continue
            lines.append(f"{index}: " + "；".join(
                f"{w.raw['slot']}={w.damage}伤{'抛' if w.gravity else '直'}"
                + ("（当前）" if machine.weapon is not None
                   and w.id == machine.weapon.id else "")
                for w in choices))
        if not lines:
            return "房里没有 bot。"
        # ★ 尾巴要短：聊天框一次只看得见 4 行，长行折出来的行同样占额度（§20）。
        mode = int(getattr(room, "bot_weapon_slot", 0) or 0)
        room_mode = "自动" if mode == 0 else f"锁{mode}号"
        return ("  ".join(lines) + f"  房间={room_mode}; "
                "/w 0 自动; /w M 锁定")
    try:
        slot = int(str(args[-1]).strip())
    except (TypeError, ValueError):
        return f"武器槽 {args[-1]!r} 不是数字。"
    if slot not in (0, 1, 2, 3):
        return (f"没有能用的 {slot} 号武器槽；只能是 0~3"
                "（0 = 自动换枪）。")
    # 只给一个参数 = 房间级模式，当前及后来加入的 bot 一起继承。
    if len(args) == 1:
        room.bot_weapon_slot = slot
        seats = room.bot_seats()
        if not seats:
            mode = "自动换枪" if slot == 0 else f"锁定 {slot} 号武器"
            conn.room_system_chat(f"房间 bot 已设为{mode}；后来加入的 bot 也会继承。")
            return None
        done, failed = [], []
        for index in seats:
            told, error = _apply_gun(conn, room, index, slot)
            (failed if error else done).append(error or told)
        if done:
            conn.room_system_chat("；".join(done))
        # ★ 换不了的（角色缺这个槽）单独回给房主，别刷全房间。
        for error in failed:
            conn.send_system_chat(f"换不了武器：{error}")
        return None
    index, error = _seat_arg(args)
    if error:
        return f"/w 用法：/w [座位号] <武器槽 0~3>。{error}"
    told, error = _apply_gun(conn, room, index, slot)
    if error:
        return f"换不了武器：{error}"
    conn.room_system_chat(told)
    return None


def _cmd_help(conn, room, args):
    """★ 房间里和战斗中给的是**两套**：聊天框一次只看得见 4 行（§20），
    而战斗中那几条改房间状态的命令本来就会被拒（`MUTATING_COMMANDS`）。"""
    for line in (BATTLE_HELP_LINES if room.is_playing() else HELP_LINES):
        conn.send_system_chat(line)
    return None


#: 命令名 -> 处理函数。
#:
#: ★★ 名字全部是**一个字母**（用户 2026-08-28 要的，D56）：这些命令是在
#: **游戏里的聊天框**敲的，打字要占住键盘、bot 那边还在打你 —— 每多一个
#: 字母都是实打实的代价。`/del` 整条删掉了，踢 bot 用**客户端自带的踢人**。
COMMANDS = {
    "a": _cmd_add,
    "c": _cmd_char,
    "t": _cmd_team,
    "team": _cmd_team_alias,
    "r": _cmd_ready,
    "s": _cmd_easy,
    "m": _cmd_medium,
    "h": _cmd_hard,
    "w": _cmd_gun,
    "hold": _cmd_hold,
    "dash": _cmd_dash,
    "noboom": _cmd_noboom,
    "slow": _cmd_slow,
    "help": _cmd_help,
    "?": _cmd_help,
}

#: 改房间状态的命令 —— 游戏中一律拒绝。`/help` 不在里面，随时能看。
#: `team` 也不在里面：它只是一行「请改用 /t」的提示，什么都不改。
MUTATING_COMMANDS = ("a", "c", "t", "r")


# ----------------------------------------------------------------------------
# 战斗中：让 bot 在别人屏幕上动起来（M3）
# ----------------------------------------------------------------------------
#: bot 跟在真人**多远**的后面（游戏内坐标单位）。
#:
#: 房里有多个 bot 时按座位次序排队（第 N 个 bot 跟 N 倍远），免得几个 bot
#: 叠在同一个点上变成一个人。
BOT_FOLLOW_DISTANCE = 120.0

# ----------------------------------------------------------------------------
# 开火（M3b）
# ----------------------------------------------------------------------------
#: ★ **服务端得整个当射手**（D28）：客户端只替「自己的」和「中立/怪的」弹体
#: 做命中判定并广播 `rpExplode`（守卫 `IsMine || IsNeutral` 在 `0x47eb4e`），
#: 而 bot 没有本机 ⇒ 它只发 `rpFire` 的话没有一台会替它算爆炸，
#: 子弹一直飞、一滴血不掉（§42）。
#:
#: 所以下面这一套是「射手那台机器」原本要干的活：挑目标、判遮挡、
#: 算命中点、算伤害、发 `rpExplode`。

#: ★★ bot 的**交战距离**上限（世界单位）。
#:
#: 原版**没有**「射程」这个字段（§44）：子弹一直飞到撞地形或者飞出图外，
#: 玩家武器一把 `LifeTime` 都没填。所以「多远才开枪」这件事得从**别处**
#: 找出处 —— 会话 14 是这么定的（§48）：
#:
#: 把 41 个真人对战语料里的 `rpFire → rpExplode` 按几何配对，
#: 挑出「打中了角色」的那 **247 发**，量开火点到爆炸点的距离：
#:
#:     p10=264   p25=413   中位=616   p75=786   p90=915   p99=1015   最大=1163
#:
#: ⇒ **原版玩家几乎不在 1000 个单位以外打中人**。取 p99 = 1000，
#: bot 的交战距离就和真人的分布一致，而不是「贴脸才开枪」。
#:
#: ★ 会话 13 之前这里是 `LockonRange`（80~120）—— 那是**自动瞄准**的作用
#: 距离，不是射程；120 只有小半个屏幕宽（地图宽 1500~2848），
#: 表现就是用户 2026-08-26 报的「距离远了 bot 就不开枪，站在身边才开枪」。
BOT_ENGAGE_RANGE = 1000.0

#: 判「这一发会不会被墙挡住」时，沿着弹道每隔多少个单位采一次样。
#: 和 `mapdata.line_blocked()` 的默认步长同口径。
BOT_LINE_STEP = 4

#: ★ 瞄准点**不再是一个常量**了（§65）：以前这里有个 `BOT_AIM_HEIGHT`
#: （先是 20、会话 18 抬到 57），因为那时候命中是服务端硬判的 ——
#: 瞄哪儿都打得中，那个数只决定爆炸特效画在哪。
#: 现在命中是**真判**的，瞄准点必须落在对方的碰撞圆里，所以改成问
#: `chrprops`：瞄**身体那个圆的圆心**（三个圆里最大的一个），
#: 每个角色、蹲着还是站着都不一样。见 `_aim_point()`。

#: ★★★ 枪口相对**自己落脚点**的偏移 —— 这两个数是**实测**出来的（§62）。
#:
#: 会话 18 在客户端 hook 里把真人自己那一发 `rpFire` 的发射点和他角色当时的
#: 坐标（`[char+0x34]/[0x38]`）并排打出来，量到的是
#: **前方 43、上方 57**（角色 0「泰尔」，1 号枪）—— 那就是枪管的位置。
#:
#: 原来这里复用 `BOT_AIM_HEIGHT`（20），意思是「角色多高的一半」，结果枪口落在
#: **自己膝盖那一格**、而且还往身后缩了几个单位。它挨着自己和目标的碰撞盒，
#: 弹体**第一次推进就撞掉**：实机日志里 bot 的每一颗弹体都只被推进 **1 帧**、
#: 位置一步没动过就没了（同一局里真人自己的子弹连飞 5 帧以上）。
#: 那正是「别人看不见 bot 的子弹」—— 它根本没有行程。
#:
#: ★ 这是**几何量**（枪管在模型上的位置），不是时序阈值，铁律 10 管不着它。
#: ⚠ 严格说每个角色的骨骼位置略有不同，这里取实测的那一组当统一值 ——
#: 差几个单位不影响「弹体在不在两个碰撞盒外面」这件事。
BOT_MUZZLE_HEIGHT = 57.0
BOT_MUZZLE_FORWARD = 43.0


def _muzzle(x, y, toward_x):
    """bot 站在 `(x, y)`（落脚点）、朝 `toward_x` 开枪时，枪口在哪。

    ★ 横向偏移**跟着朝向翻**：角色永远面朝准星（§37），枪口就在身前。
    往身后放的话弹体一出生就在自己的碰撞盒里。
    """
    forward = BOT_MUZZLE_FORWARD if toward_x >= x else -BOT_MUZZLE_FORWARD
    return (x + forward, y - BOT_MUZZLE_HEIGHT)

#: ★★ **临时诊断开关**（会话 17，M3b 收口后连同 `/noboom` `/slow` 一起删）：
#: 环境变量 `BOT_DIAG_FIRE_ANYWHERE=1` 时，`_fire_target()` 无视
#: **交战距离**和**地形遮挡**，隔多远、隔几堵墙都照样开枪。
#:
#: 只为一件事：把「bot 的弹体在收方长什么样」这一份客户端日志取到手（§58）。
#: 取证的人操纵不了角色走位（游戏用 DirectInput 读键盘，`keybd_event`
#: 注入的方向键只能让角色转身、走不动），没这个开关就凑不到 1000 单位以内。
#: ⚠ **不要在正常游玩时打开** —— bot 会隔着整张图和墙壁乱开枪。
BOT_DIAG_FIRE_ANYWHERE = os.environ.get(
    "BOT_DIAG_FIRE_ANYWHERE", "") not in ("", "0")
if BOT_DIAG_FIRE_ANYWHERE:
    asynclog.emit("[bot] ★★ BOT_DIAG_FIRE_ANYWHERE 已开 —— bot 会无视交战距离和地形"
                  "遮挡开枪，并逐帧报「为什么不开枪」。这是取证用的临时开关，"
                  "正常游玩别开。")


def _followable_humans(room):
    """房里**报过位置**的真人。没有一个就返回空表。

    ★ 判据是「他的心跳到过服务端」这个事实，不是「他在座」——
    还在加载、或者中继刚断的人，位置是不可信的。
    """
    out = []
    for index in room.human_seats():
        seat = room.seats[index]
        conn = None if seat is None else seat.conn
        if conn is not None and getattr(conn, "sync_trail", None):
            out.append(conn)
    return out


def _follow_target(room, machine):
    """这个 bot 该跟谁：离它最近的真人；它还不知道自己在哪就跟座位号最小的。"""
    humans = _followable_humans(room)
    if not humans:
        return None
    if machine.battle_pos is None:
        return humans[0]
    x, y = machine.battle_pos

    def distance(conn):
        hx, hy = conn.sync_trail[-1][:2]
        return (hx - x) ** 2 + (hy - y) ** 2

    return min(humans, key=distance)


def trail_point(trail, distance):
    """沿着 `trail` 从最新那点往回走 `distance`，返回落脚点。

    返回 `(x, y, jumped, on_ground, vx, vy, fast_run, crouch)`：坐标是插出来
    的，后六个是真人**在这一段**的原样事实（起没起跳、踩地还是腾空、腾空时
    的速度、是不是按着右键冲刺、是不是蹲着）。
    轨迹比 `distance` 短就返回最老的那点（bot 刚进图、真人还没走几步时
    就是这种情况）。

    ★★ 后三个（`on_ground` / `vx` / `vy`）**必须原样抄给心跳**，不能自己
    从位移反推（V0.3 §35）：真人踩在地上走的时候速度两格是 **0**，反推出
    一个非零速度会让收方拿它自己往前推算，和下一发心跳里的坐标打架 ——
    就是「走一步停一下、像在抽搐」那个症状，而且走路动画根本不播。

    ★ 为什么是「回放真人走过的点」而不是「自己算一条路」：服务端**一点地图
    几何都没有**（M4 才有）。真人刚刚站过的地方一定是合法地面，照着踩就不会
    掉进地形里、也不会飘在半空 —— 连跳跃的抛物线都是现成的（D16）。

    ★★ **落脚点在两个采样点之间插值，不吸附到采样点上**（V0.3 §32）。
    吸附的那一版会一顿一顿：真人每报一个新点，「往回 120」落在哪个采样点上
    是**跳变**的 —— 有时原地不动、有时一下跨两个点。插值之后 bot 每帧前进
    的距离**恒等于真人这一帧前进的距离**（跟随距离是常数），节奏和真人一样。
    插的那一段是真人 125 ms 内实际走过的直线，落在地形里的风险可以忽略
    （跳跃抛物线的弦最多沉下去几个单位）。

    ⚠ 起跳标记**和运动状态**都取**刚刚走过的那个采样点**（插值区间靠**老**
    的那一端）：
    bot 是沿着老 -> 新的方向重走这条路的，落脚点落在某一段里 = 它这一帧
    正好越过了那一段的老端点。真人在更前面（更新）的点上跳，标记就还轮不到
    ——「一起跳、还差 120 的 bot 立刻跟着跳」那种抽搐不会发生。
    ★ 每一帧**恰好**越过一个采样点（bot 的滞后距离是常数，头往前挪多少
    它就挪多少），所以同一个起跳标记不会被报两次。
    """
    points = list(trail)
    if not points:
        return None
    walked = 0.0
    index = len(points) - 1
    while index > 0 and walked < distance:
        x0, y0 = points[index - 1][0], points[index - 1][1]
        x1, y1 = points[index][0], points[index][1]
        step = math.hypot(x1 - x0, y1 - y0)
        if walked + step >= distance and step > 0:
            # 落脚点就在 `points[index-1] -> points[index]` 这一段里面。
            # `ratio` = 从**新**的那一端往回退多少（1 = 正好落在 index-1 上）。
            ratio = (distance - walked) / step
            return (x1 + (x0 - x1) * ratio, y1 + (y0 - y1) * ratio,
                    *_motion_of(points[index - 1]))
        walked += step
        index -= 1
    point = points[index]
    return (float(point[0]), float(point[1]), *_motion_of(point))


def _motion_of(point):
    """一个轨迹点的 `(jumped, on_ground, vx, vy, fast_run, crouch)`。

    ★ 老式三元组（单测里手搓的假轨迹、以及本版之前落盘的那种）没有后面几个
    字段：那时候补上「踩在地上、速度 0、不冲刺」—— 那是**地面行走**，也正是
    没有更多信息时唯一安全的假设（腾空却说踩地，最多少一段抛物线姿势；
    反过来说腾空则会让收方拿一个假速度推算，直接抽搐）。
    """
    jumped = point[2] if len(point) > 2 else 0
    if len(point) >= 6:
        fast_run = bool(point[6]) if len(point) >= 7 else False
        crouch = bool(point[7]) if len(point) >= 8 else False
        return jumped, bool(point[3]), point[4], point[5], fast_run, crouch
    return jumped, True, 0, 0, False, False


def _walk_direction(previous, x):
    """这一帧 bot 往哪边走：`+1` 右 / `−1` 左 / `0` 没挪窝。

    ★★ 比的是**线上那个 i16**，不是浮点落脚点（§39）。收方看得见的只有
    包里那个截断过的坐标 —— 浮点上挪了 0.3、线上一动没动，却对它说
    「我按着右键」，收方就会按走路速度把角色往前推，再被下一发心跳拉回来：
    那正是 §35 那种「推一下拉一下」的抽搐。

    ★ 这一格是 bot **自己这一帧的位移**，不是抄真人的。抄真人的按键在
    「bot 还落后一大截、真人已经在走」的那几帧上会说谎（bot 明明站着不动，
    却报「我在走」）—— 而 `on_ground` / 速度那三格必须抄真人是因为
    **服务端没有地图几何**，按键这件事 bot 自己知道得最准。
    """
    if previous is None:
        return 0
    was, now = botsync.clamp_i16(previous[0]), botsync.clamp_i16(x)
    if now > was:
        return botsync.FACING_RIGHT
    if now < was:
        return botsync.FACING_LEFT
    return 0


def _current_map(room):
    """房间**这一刻**在哪张图上（闯关房带 `#难度` 后缀，§140）。

    ★ 只有一份实现，在 `gameserver.current_map_name()` 里 —— 以前这儿抄了
      一份一模一样的，结果「闯关按难度选图」那一手只补在一边就会漂移。
    """
    return gameserver.current_map_name(room)


def _breakables(room):
    """这一局的可破坏物账（`botbreak.Ledger`）；还没开局返回 `None`。

    ★ 和血量台账（`_health`）一样懒挂在 `RoomQuest` 上：它跟着「一局」
      生灭，回房间时整个丢掉。
    """
    quest = None if room is None else room.quest
    if quest is None:
        return None
    ledger = getattr(quest, "bot_breakables", None)
    if ledger is None:
        ledger = quest.bot_breakables = botbreak.Ledger()
    return ledger


def _refresh_breakables(room):
    """可破坏物的状态翻了没有；翻了就把路线作废、把新那张图预热掉（§138）。

    ★ 判据是**地形对象换没换**（`variant()` 是 memo 化的，同一个存活集合
      永远同一个对象）——「碎了」和「长回来了」两个方向自动都覆盖到，
      不用各写一遍，也不用比集合。
    ★ 「长回来」这一下是在 `Ledger.alive()` 里判的，而这个函数每一帧都会
      被问到 —— 帧本身是真人的同步包驱动的，没有另起定时器。
    """
    quest = None if room is None else room.quest
    if quest is None:
        return
    terrain = _terrain(room)
    if terrain is None or not terrain.breakables:
        return
    if getattr(quest, "bot_terrain", None) is terrain:
        return
    quest.bot_terrain = terrain
    # 路线是在**上一份**地形上算出来的，整个作废。
    for index in room.bot_seats():
        seat = room.seats[index]
        machine = None if seat is None else seat.conn
        if isinstance(machine, BotConn):
            _clear_navigation(machine)
            machine.path_breakable = None
            machine.path_breakable_prefix = []
            machine.path_breakable_only = False
    # ★★★★★ **战斗中不预热**（V0.3 §163 / D124）。这里原来是
    #   `warm_navigation(room, "破坏物变化")` —— 每翻一次罐子就把整张图重算
    #   一遍 × 每个角色尺度。而它和 `botplan` 那条线程做的是**同一份活**：
    #
    #       Esperan03  整图预热          981 ms
    #       Esperan03  botplan 冷缓存第一发  931 ms（第二发 7.9 ms）
    #
    #   ⇒ 预热在这儿一点都不省，纯属抢着把同一份活多做几遍，而代价是
    #   把 GIL 占住、饿死每条连接那条发送线程（D108）：下行积压 1~2.5 秒，
    #   `rpExplode` 迟到 ⇒ 收方那颗弹体早按自己的引信自灭了 ⇒ 整包静默丢弃
    #   ⇒ 句柄永久错开 ⇒ 「bot 打我没有伤害」（§42/§147/§161）。
    #   `Esperan03` 一局翻二十来次，实测三条预热线程一起转的时候，
    #   子弹积压了 2314 / 2294 / 1523 ms，全在预热收尾那一刻一起到达。
    #
    #   ★ 弹道粗网格不用管：variant 是从母地形补出来的，实测 **0.0 ms**。
    #   ★ 开局 / 换图那一次**照旧预热** —— 那时候房间循环还没起步、真人还在
    #     读图，CPU 是空着的，正是该把这份活做掉的时候。


def _terrain(room):
    """★★★ 房间这一刻的地形 —— **按可破坏物碎了哪几件挑那一份**（§138）。

    `mapdata.load()` 给的是「全都完好」那一份；碎掉的那几件要摘掉，
    否则 bot 会觉得已经被打开的路还堵着（反过来，不摘就是原来那个 bug
    的另一半）。`variant()` 是 memo 化的，同一个状态永远同一个对象，
    所以 `botnav` 的可达图缓存该复用的照样复用。

    ⚠ 一切要地形的地方都必须走这里，不许再直接 `mapdata.load()`。
    """
    terrain = mapdata.load(_current_map(room))
    if terrain is None or not terrain.breakables:
        return terrain
    ledger = _breakables(room)
    if ledger is None:
        return terrain
    return terrain.variant(ledger.alive(terrain))


#: ★★★ **伤害翻倍的那两个游戏模式**（§87）。
#:
#: 出处 `0x4806bf`（射手那台机器上，所有伤害的必经之路）开头三句：
#:
#:     004806d8  call 0x409e0a          ; 游戏模式 = 房间描述符 arguments[1]
#:     004806dd  cmp eax, 3 ; je        ; ★ 夺分
#:     004806e9  cmp eax, 5 ; jne       ; ★ 还有一个 5
#:     004806f1  shl dword ptr [eax], 1 ; ★★ 伤害整数**左移一位 = ×2**
#:
#: 用户 2026-08-28 拿火焰自己烧自己实测：生存模式 10、夺分模式 20，
#: 一位不差。★ 模式 5 我们的服务端造不出来（PvP 只有 0~3），照抄留着。
BOT_DOUBLE_DAMAGE_MODES = (3, 5)


def _pvp_game_mode(room):
    """这一局的游戏模式号（房间描述符 `arguments[1]`）；读不出来返回 `None`。

    ★ 读不出来一律当「不翻倍」——闯关房的 `arguments` 不是这套含义，
    宁可少乘也不要凭空给怪加一倍伤害。
    """
    arguments = getattr(room, "arguments", None) or ()
    if len(arguments) <= 1:
        return None
    try:
        return int(arguments[1])
    except (TypeError, ValueError):
        return None


def _damage_scale(room):
    """这一局伤害要乘几（§87）。夺分（3）和模式 5 是 **2**，其余是 1。

    ★★ 这一步在原版里是**射手那台机器**做的（`0x4806bf` 在
    `IsMine` 门后面），改的是要塞进 `rpExplode +24` / `rpSplashDamaged +8`
    的那个整数 —— 收方拿到多少就扣多少，不重算（§42）。
    bot 没有本机 ⇒ 归服务端。

    ⚠ 同一个函数后面还有一串**装备 / 附魔 / 宠物**的百分比加成
    （`0x407014(角色, 属性号, -1)` 取百分比，`damage × (100+pct) × 0.01`，
    属性号 1 / 7 / 8 / 2 …）。bot 是白板号，一个都没有 —— 真人身上有，
    那部分归他们自己那台机器算，服务端不碰。
    """
    return 2 if _pvp_game_mode(room) in BOT_DOUBLE_DAMAGE_MODES else 1


#: ★★★ **夺分模式独有的两条 ×0.75**（§89）——「只减，不加」，而且
#: **只作用在直接命中上**。
#:
#: 出处 `0x47e618`（`Projectile` 虚表槽 `+0x128`，19 个弹体类全指向它），
#: `Projectile::OnHit` 在 `0x47ec5b` 调它，紧接着 `0x4806bf` 那一发 ×2：
#:
#:     0047e66d  cmp eax, 3 ; jne 出口     ; ★ 只有模式 3（模式 5 **不减**）
#:     0047e6c8  mov eax, [0x6e9b94]       ; 视口
#:     0047e6cd  fild dword [eax+0x30]     ; ★ 视口宽度 = 1024
#:     0047e6dd  jne 跳过                   ; 距离 <= 它 -> 不减
#:     0047e6df  fild [ebx] ; or [ebp+0xc], 8 ; fmul [0x6938c8] ; ftol
#:     0047e6f5  cmp byte [目标+0x128], 0  ; ★ 目标**踩在地上**
#:     0047e700  or [ebp+0xc], 4 ; fmul [0x6938c8] ; ftol
#:
#: 两条各自 ×0.75、**各自朝零截断**，累乘（`0x6938c8` = 0.75）。
#: 那两个 `or` 写的正是 `rpExplode +20` 的 flags —— 用户 2026-08-28 那一局
#: 里他自己那发苹果弹的包就是 `flags 4 伤害 30`（= 20 × 2 × 0.75），
#: 而 bot 打他是 40（没减）。
#:
#: ★ **溅射 / 地面燃烧 / 近身没有这两条**：`SplashDamage` / `Flame` 的
#: 虚表槽 `+0x128` 直接指向 `0x4806bf`（只有 ×2），`DashDamage` 指向
#: `0x481dfd`（×2 + 装备加成），三个都不经过 `0x47e618`。
BOT_LONG_SHOT_MODES = (3,)

#: ★ 「离得远」的那条门槛 = **视口宽度**，`ViewPort::Init(0, 0, 0x400, 0x300)`
#: 把 `0x400` 写进 `[视口+0x30]`（`0x5cc80b`，全镜像只有这一处构造）。
#:
#: 语料实证（§89）：68 个夺分局的 5625 发直接命中里，按「射手心跳坐标 →
#: 爆点」当距离，**1024 这个门槛只错分 35 发（0.62%）**，而最优门槛是
#: 1040 —— 两者在噪声里分不开（爆点是打在碰撞圆上的那一点，不是目标原点）。
BOT_LONG_SHOT_RANGE = 1024.0

#: 那两条各自乘多少（`0x6938c8`）。
BOT_DAMAGE_PENALTY = 0.75


def _direct_hit_damage(room, machine, weapon, region, victim_seat,
                       damage_ratio=1.0):
    """**直接命中**要填进 `rpExplode +24` 的伤害（§87 + §89）。

    三步，顺序和 `0x47ec5b` 那条链一模一样：

    1. 按部位取档（`Damage` / `HeadDamage` / `LegsDamage`）；
    2. 夺分 / 模式 5 **×2**（`0x4806f1` 的 `shl`）；
    3. ★ **只有模式 3**：目标离得比一个视口宽（1024）还远 ×0.75、
       目标**踩在地上** ×0.75 —— 各自朝零截断，两条都成立就乘两次。

    ⚠ 距离量的是**两个角色原点之间**（`vft+8` = `GetPos`，也就是脚下那点），
    不是爆点到目标。踩没踩地读的是 `[char+0x128]`，服务端这边就是心跳
    位域的 bit2（§5.6）—— 读不出来就**不减**，宁可少扣也不要凭空扣。
    """
    # ★ 强力射击的 `DamageRatio`（§117）—— 原版也是在射手这边算完才塞进包的。
    damage = int(weapon.damage_for(region) * damage_ratio) * _damage_scale(room)
    if _pvp_game_mode(room) not in BOT_LONG_SHOT_MODES:
        return damage
    shooter = machine.battle_pos
    victim = _seat_body(room, victim_seat)
    if shooter is not None and victim is not None:
        span = math.hypot(victim[0] - shooter[0], victim[1] - shooter[1])
        if span > BOT_LONG_SHOT_RANGE:
            damage = int(damage * BOT_DAMAGE_PENALTY)
    if _seat_on_ground(room, victim_seat):
        damage = int(damage * BOT_DAMAGE_PENALTY)
    return damage


# ---------------------------------------------------------------------------
# ★★★ 击退（§92）—— 谁挨了打就得往后飞
# ---------------------------------------------------------------------------
#: 击退方向的「上抬量」：先把来向归一化，**y 减去 0.7**，再归一化一次。
#:
#: 出处 `0x481003`（所有伤害源的虚表槽 `+0x148`，19 个弹体类 +
#: `SplashDamage` / `Flame` / `DashDamage` 全指向它）：
#:
#:     0048100f  D3DXVec2Normalize(&d, v)
#:     00481017  d.y -= [0x6937e0]        ; ★ 0.7
#:     00481025  D3DXVec2Normalize(&d, &d)
#:     004810a8  out = d × 强度(伤害)
#:
#: y 轴向下为正 ⇒ 减 0.7 = **往上顶**。所以任何击退都带一点向上的分量，
#: 这就是「被打中会飞起来」的来源。
KNOCKBACK_LIFT = 0.7

#: ★★ 击退**强度**的阶梯：`(伤害上界, 强度)`，第一个「伤害 < 上界」的档说了算。
#:
#: `0x48102f` 起那五发 `fcom` + `jp`（相等走下一档）。语料 13160 发
#: `rpSplashDamaged` 逐档对得死：伤害 1~9 全是 4.0（5637 发）、
#: 10~19 全是 8.0（3908 发）、20 起 15.0。
#:
#: ⚠ 喂进这道阶梯的伤害**是模式倍率之前**那个数（溅射这条路上
#: `0x4858a2 push eax` 取的是 `+0x134` 的返回值，`×2` 是后面
#: `+0x128` 才乘的，§90）。语料佐证：同一个「包里伤害 20」在生存局是
#: 15.0、在夺分局是 8.0（因为夺分那边喂进去的是 10）。
KNOCKBACK_STEPS = ((10.0, 4.0), (20.0, 8.0), (40.0, 15.0),
                   (80.0, 20.0), (160.0, 40.0))

#: 伤害 >= 160 时的强度（`0x48102a` 那发 `fld1` 留下来的 1.0）。
#: 现实里够不着 —— 照抄留着。
KNOCKBACK_OVERKILL = 1.0

#: ★ 地面燃烧的击退是**常量**，不走阶梯（`Flame` 虚表槽 `+0x134` =
#: `0x485e4d`：`mov [ebp-4], 0xc1000000` = −8.0，x 恒 0）。
#: 语料 1164 发 `(0.0, -8.0)`，一发例外都没有。
FIRE_KNOCKBACK = (0.0, -8.0)

#: ★ 近身冲刺的击退也是常量（`DashDamage` 虚表槽 `+0x134` = `0x481d6e`：
#: `fild [this+0x308] × [0x69381c]=15`、y = `0xc1200000` = −10）。
#: `[+0x308]` 是朝向（±1）⇒ 语料里就是 `(±15, -10)`，1347 发。
DASH_KNOCKBACK = (15.0, -10.0)

#: ★★ 「吃不吃击退」的门槛（`0x50f7ca` 里那两处 `cmp ..., 0xa`）。
#:
#: 收侧那个函数按伤害分两档反应（`kind == 0` 这一路，直接命中和溅射都是它）：
#:
#:     伤害 >= 10 -> 甲：伤害 **> 10** 才真的加速度；**无论如何都离地**
#:     伤害 <  10 -> 在地上：只沿地面滑 `push.x × 3`（`0x50d9a7`），不离地
#:                   腾空中：★ **一点都不推**（走的也是甲档，而甲档里
#:                   「伤害 <= 10」那一路要求「在地上且 v.x == 0」）
KNOCKBACK_MIN_DAMAGE = 10

#: 甲档里「伤害正好等于 10」时给 `v.y` 的下限（`0x693bb0` = −10）——
#: 只有「站在地上且水平速度为 0」时才夹（`0x50f884`）。
KNOCKBACK_MIN_LIFT = -10.0

#: 乙档在地上时横向滑多远的系数（`0x50f84f: fmul [0x6937c8]` = 3.0）。
KNOCKBACK_SLIDE = 3.0


def knockback_strength(damage):
    """这么多伤害该把人推多快（`0x481003` 那五发 `fcom`）。"""
    value = float(damage)
    for limit, strength in KNOCKBACK_STEPS:
        if value < limit:
            return strength
    return KNOCKBACK_OVERKILL


def knockback_vector(dx, dy, damage):
    """来向 `(dx, dy)` + 伤害 -> 击退矢量（`0x481003`）。

    `(dx, dy)` 是**冲击的来向**：直接命中取**弹体此刻的速度**
    （`0x49285c` 读 `[proj+0x120]/[+0x124]`），溅射取**爆点指向目标身体
    圆心**的那条（`0x4857d0`）。两个都在这里先归一化、再上抬、再归一化。

    ★ 来向是零向量时原版把 `dy` 强置成 **−1**（`0x485805`，「正上方顶飞」）。
    """
    span = math.hypot(dx, dy)
    if span <= 0.0:
        ux, uy = 0.0, -1.0
    else:
        ux, uy = dx / span, dy / span
    uy -= KNOCKBACK_LIFT
    span = math.hypot(ux, uy)
    if span <= 0.0:
        return (0.0, 0.0)
    strength = knockback_strength(damage)
    return (ux / span * strength, uy / span * strength)


#: ★ 真人的一发子弹在「配爆炸」这本账上留多久（tick）。
#:
#: 不是时序阈值：它是**弹道本身的上界** —— 图的对角线除以最慢的速率，
#: `_shell_max_ticks()` 给 bot 自己的弹体算的也是这个量级。这里取一个
#: 宽松的固定上界只是为了让账本不会无限长；配不上就不给击退，不影响伤害。
BOT_PEER_SHOT_TICKS = 200

#: 账本里每个座位最多留几发。散弹一次 3 颗、苹果雷炸开 4 片，留 16 发
#: 足够覆盖「同时在飞」的最坏情况。
BOT_PEER_SHOT_KEEP = 16

#: 拿爆点去配开火记录时，弹道预测的 y 允许差多远（世界单位）。
#:
#: ★ 出处是 §76 量过的「客户端 vs 服务端同帧偏差中位 19.5 / 26.0」——
#: 取 4 倍留够余量。配不上就**不给击退**（宁可少顶一下，也不要按一条
#: 不相干的弹道把 bot 往反方向甩）。
BOT_PEER_SHOT_TOLERANCE = 120.0


#: ★★ `rpFire` body `+0` 说这一发是**谁**打的：`10 + 座位号`是玩家，
#: **怪 / 中立是 20**（§23 的 7040 发实证）。`HandleToOwner` 那条路上中立是
#: 30（§42 末尾那条勘误），两个都收着。
MOB_FIRE_SOURCES = (20, 30)

#: ★★ 怪 / boss 替发的 `rpFire`，枪口坐标归给「最近的那只已知怪」的距离上限。
#:
#: 载具 boss 的部件（`MachineGun` / `Cannon1` / `Cannon2`…）各有各的枪口，
#: 但都装在同一台车上 —— 实机（2026-08-30 第四轮，§141）量下来：
#: 机关枪 (871.7, 430.0)、炮 (765.9, 517.9)，两个部件相距 **106**，
#: 本体行（真人第一发命中建的）到部件枪口 ~100。300 够把整车都归进去。
MOB_GUN_REACH = 300.0

#: ★ 判「打没打中怪」用的碰撞半径。`Mob.ini` 24 个怪里 **9 个是 40**
#: （唯一的众数；15 和 65 各只有一只），而服务端**没法把句柄映射回怪的类型**
#: —— 那要连刷怪组和关卡脚本一起解。所以统一按众数算。
#: ⚠ 这一格是实机最该盯的：觉得 bot 打怪「明明没碰到也掉血」就调小它。
MOB_HIT_RADIUS = 40.0

#: `rpAiMsg` 里那几个键（明文，见 `botsync.parse_ai_message`）。
AI_MSG_STATE = "setState"
AI_STATE_DEATH = "death"


def note_ai_message(room, handle, fields):
    """收下一发 `rpAiMsg` —— **怪的位置就是这么广播的**（§125）。

    ## 这一条推翻了会话 40 上半场的结论

    上半场我扫了一遍上行流，看见心跳的「角色数」字段恒为 1，就下结论说
    「怪的位置一个包都不广播」。**错的**：位置不在心跳里，在 `rpAiMsg` 里，
    而那一发的 body 是一段**明文 `key=value`**，静态分析看组包点只看到
    「写了 8 字节的头」，正文是另一个地方拼进去的（§23 那条 ⚠ 早就写了
    「实测长度 48 / 91 / 139 变长」，我没去核）。

    用户 2026-08-29 的反问才是对的：真人之间能合作、都看得见怪，
    那一定有广播。

    ## 怎么记

    * `msgType=setState` 带 `posX` / `posY` ⇒ 更新位置；
    * `state=death` ⇒ 这只怪没了，**删掉**；
    * `targetChrSlot` ⇒ 它在追谁（留着，以后做仇恨/走位可能要）。

    ★ 全是**事件**驱动，一个定时器都没有（铁律 10）：位置由控制者报，
    死亡由控制者报，换图由 `RoomQuest` 清。
    """
    quest = None if room is None else room.quest
    table = getattr(quest, "mobs", None) if quest is not None else None
    if table is None or not handle:
        return
    # ★★ **boss 房是从这一发认出来的**（§141）：进 boss 图时控制者广播
    #    `fileName=data/quest/questNN/questNNSxboss.ini` + `type=start`
    #    （普通关卡的 `questNNS1enemy.ini` / `questNNS1clear.ini` 也是一个
    #    格式，靠文件名里的 `boss` 分）。置起 `quest.boss_room` 之后
    #    牵引绳 / 跟随点整条停用，bot 只管打 boss。
    name = fields.get("fileName") or ""
    if name and fields.get("type") == "start":
        if "boss" in name.lower() and not getattr(quest, "boss_room", False):
            quest.boss_room = True
            asynclog.emit(f"[{gameserver.ts()}]    进入 boss 房（{name}）—— "
                          f"牵引绳停用，bot 只管打 boss")
        return
    if fields.get("msgType") != AI_MSG_STATE:
        return
    if fields.get("state") == AI_STATE_DEATH:
        table.pop(handle, None)
        return
    row = table.get(handle)
    x = _ai_float(fields, "posX", row[0] if row else None)
    y = _ai_float(fields, "posY", row[1] if row else None)
    if x is None or y is None:
        # 只报了状态没报坐标（boss 的几个阶段就是这样）——- 还没见过它就不建表。
        if row is None:
            return
        x, y = row[0], row[1]
    table[handle] = [x, y, fields.get("state"),
                     _ai_int(fields, "targetChrSlot")]


def _ai_float(fields, key, fallback=None):
    try:
        return float(fields[key])
    except (KeyError, TypeError, ValueError):
        return fallback


def note_mob_gun_muzzle(room, x, y):
    """★★★ 怪 / boss 的枪响了 —— 枪口坐标是一次**它的位置采样**（§141）。

    ## 这就是「不报坐标的那族 boss」的位置同步

    用户 2026-08-30 第四轮：「真人是可以合作打 boss 的，所有人都能同步
    看到 boss 的状态和招式，既然真人能合作，那一定有某种 boss 状态同步
    机制。」—— **他是对的**，机制分两层：

    * **招式 / 阶段**：`rpAiMsg` 的 `setState` 广播 `phase` / `state`
      （`machinegun` / `cannon1` / `move`… 都是招式名），各客户端**本地
      锁步演播**动画 —— 这就是「所有人都看得到 boss 在挥什么招」；
    * **位置**：两族 boss 两种做法 ——
      * 报坐标的（语料 Quest02 的吊车 boss，62 发里 23 发带 `posX`）
        直接随 `setState` 广播；
      * **载具 boss 那一族**（quest02 的 `jiksa/melee/powerwave`、
        quest03 的 `machinegun/cannon1/cannon2`，和 §141 实机那场同族）
        `setState` **从不带 posX** —— 但它们的枪是**控制者机器替它发的**
        （`rpFire body+0 == 20`），**每一发都带着部件的世界坐标**
        （实机 35 发机关枪 / 炮，全钉在 (871.7, 430.0) 和
        (765.9, 517.9) 两个部件上）。位置同步就在这条流里。

    ## 归属

    `rpFire` 里没有怪的句柄，归给**怪物表里离枪口最近的那只**
    （`MOB_GUN_REACH` 以内才算 —— 部件都装在同一台车上）。
    boss 房里一只都还不知道（真人还没打中过）就把最近一次枪口存进
    `quest.boss_gun`：开不了枪（没有句柄），但 bot 的走位目标有了。
    """
    quest = None if room is None else room.quest
    table = getattr(quest, "mobs", None) if quest is not None else None
    if table:
        best = None
        for handle, row in table.items():
            span = math.hypot(row[0] - x, row[1] - y)
            if best is None or span < best[0]:
                best = (span, handle, row)
        if best is not None and best[0] <= MOB_GUN_REACH:
            best[2][0], best[2][1] = float(x), float(y)
            return
    if _boss_room(room):
        quest.boss_gun = (float(x), float(y))


def _ai_int(fields, key, fallback=None):
    try:
        return int(fields[key])
    except (KeyError, TypeError, ValueError):
        return fallback


def _is_breakable_handle(room, handle):
    """这个句柄是不是**当前这张图**上的一件可破坏物。

    破坏物的世界句柄是 `.map` 里原样抽出来的（`terrain.breakables`，
    §136 / §139）—— 拿它当过滤器，`rpExplode` 打中的非玩家句柄就能分清
    「箱子」和「怪 / boss」了。
    """
    terrain = _terrain(room)
    items = getattr(terrain, "breakables", ()) if terrain is not None else ()
    return any(item.handle == handle for item in items)


def note_mob_hit(room, handle, x, y, create=False):
    """有人打中了这只怪 —— 爆点是一次**额外的**位置采样（免费的精度）。

    ★ 只更新**已经在表里**的怪（`create=False`，默认）：`rpExplode` 打中的
      「非玩家句柄」里还混着破坏物之类的世界对象（语料里 509 个非玩家
      目标，只有 261 个在 AI 流里出现过），凭它建表会把箱子当成怪。

    ★★ `create=True`（§141）：**boss 从来不广播坐标** —— 它的 `setState`
      只带 `phase` / `state`（实机整场 72 发一发 posX 都没有），AI 流里
      永远等不到它。但它会**被打中**：每一发直接命中都是一次「它在哪」
      的采样，而且句柄就是收方扣血用的那个（=AI 句柄 +1，部件各自一个）。
      ★ 建表**只在 boss 房里放行**：普通关卡里的怪全部走 AI 流（§125），
      而命中的「非玩家句柄」里除了怪和可破坏物还混着**别的世界对象**
      （机关 / 场景物）—— boss 房里那些都是要打的 boss 部件，普通关卡里
      建出来的只可能是鬼目标。破坏物两头都靠 `.map` 里的世界句柄表分流。
    """
    quest = None if room is None else room.quest
    table = getattr(quest, "mobs", None) if quest is not None else None
    if table is None or not handle:
        # ★ `is None` 而不是 `not table`：空表也要进得来 —— 建表（`create`）
        #   恰恰发生在表还空着的时候（§141：boss 房的第一发命中）。
        return
    row = table.get(handle)
    if row is None:
        if (not create or not _boss_room(room)
                or _is_breakable_handle(room, handle)):
            return
        table[handle] = [float(x), float(y), None, None]
        asynclog.emit(f"[{gameserver.ts()}]    见到怪（命中建表）: 句柄 {handle} "
                      f"@ ({float(x):.0f}, {float(y):.0f})")
        return
    row[0], row[1] = float(x), float(y)


def _score_quest_damage(room, machine, damage):
    """★★ bot 打在怪身上的伤害记进**闯关分数**（V0.3 §130）。

    用户 2026-08-30：「闯关模式下，bot 打怪后，右上角 bot 没有分数，
    我希望 bot 也记分。」

    ## 分数是什么

    真人那一侧是客户端自己算好、每次加分发一发 `0x0410`（累计值，500 ms
    节流），服务端记下来再广播 `0x0415 gspUpdateQuestScore(座位, 累计值)`
    —— 右上角战绩面板读的就是 `0x0415`（§109）。bot 没有客户端 ⇒ 没人替它
    算，那一格永远是 0。

    ## 一分是多少：**打在怪身上的伤害**

    语料实证（64 份带 `0x0410` 的流）：累计分数的**增量众数是 22**
    （3703 次），后面依次是 12 / 16 / 24 / 9 / 32 —— 全都是武器伤害表里的
    数（`ch02-03` 伤害 22、`ch02-02` 16、`ch01-03` 18、`ch00-02` 20…）。
    ⇒ 加分单位就是伤害值。

    ⚠ 已知偏高一点：真人那边「超杀」的部分不算分（分数 ÷ 打怪总伤害的
    中位是 0.68），而服务端**不知道怪还剩多少血**（句柄映射不回
    `Mob.ini`），没法扣。结算不用这个数，观感优先。
    """
    if damage <= 0 or room.team_layout() != lobby_module.TEAM_LAYOUT_COOP:
        return
    machine.quest_score = int(machine.quest_score) + int(damage)
    try:
        machine.battle_broadcast(
            gameserver.build_game(
                gameserver.OP_REP_QUEST_SCORE,
                gameserver.build_rep_quest_score(machine.my_seat,
                                                 machine.quest_score)),
            reason=f"：bot 闯关分数 -> {machine.quest_score}")
    except OSError as error:
        machine.log(f"   ⚠ 闯关分数广播失败（{error!r}），服务端这边照样记")


def live_mobs(room):
    """场上还活着、位置已知的怪：`[(x, y, 句柄)]`。"""
    quest = None if room is None else room.quest
    table = getattr(quest, "mobs", None) if quest is not None else None
    if not table:
        return []
    return [(row[0], row[1], handle) for handle, row in table.items()]


class PeerShot(object):
    """真人打出去的一发（`rpFire`）。

    两个用处：给击退反推来向（§92），以及**让 bot 躲开它**（M5-E）。
    后者要知道它是**什么时候**出膛的 —— 包里没有这一格，只能用收到的时刻。
    误差就是一个网络单程，而 bot 本来就允许判断错（`dodge_error`）。
    """

    __slots__ = ("weapon", "x", "y", "shot", "at", "serial", "source")

    _next_serial = 0

    def __init__(self, weapon, x, y, shot, at=0.0, source=0):
        self.weapon = weapon
        self.x = float(x)
        self.y = float(y)
        self.shot = shot
        self.at = float(at)
        #: `rpFire body+0`：`10 + 座位号` 是玩家，**20 / 30 是怪**（§23）。
        #:   闯关里怪的子弹是**队友那台机器**替它发的，躲不躲它不能按队伍判。
        self.source = int(source)
        PeerShot._next_serial += 1
        #: 认「同一发」用的号 —— 闪避掷骰子按它去重。
        self.serial = PeerShot._next_serial


def _quest_weapon(room, ammo):
    """按 id 查一把武器 —— **怪 / boss 的枪优先查「当前关卡 + 难度」**（§141）。

    `Data/Quest/QuestNN/weapon-N.ini` 的 id 是**关卡内局部**的：
    `2003010` 在 Quest02..Quest07 里是不同的枪；同一个 id 四个难度各一份、
    **连弹速都不同**（Boss-Jiksa 12→23）。客户端进图时只加载「该关卡该
    难度」那一份照局部 id 解析 —— 服务端照同一个口径查：**quest 三维表
    优先，查不到才退主表**。

    ★★ 为什么 quest 在前（二次复审抓的）：主表里本来就有 8 个怪武器 id
      （`Soldier-*` / `Cannon-Bullet`），quest 文件是对它们的**增量覆盖**
      —— Quest03 简单的 `2003010` 是弹速 5 / 伤害 6，主表那份是
      弹速 3 / 伤害 8。先查主表的话这 8 个 id 永远拿不到关卡数值。
    """
    if room is not None:
        name = gameserver.current_map_name(room) or ""
        prefix = re.match(r"[Qq]uest(\d+)", name)
        difficulty = gameserver.room_difficulty(room)
        if difficulty is None:
            # 描述符参数缺失时退「地图名后缀」—— `current_map_name` 补的就是它。
            for number, suffix in mapdata.DIFFICULTY_SUFFIX.items():
                if name.endswith(suffix):
                    difficulty = number
                    break
        if prefix is not None and difficulty is not None:
            weapon = weapondata.get_quest(ammo, int(prefix.group(1)),
                                          difficulty)
            if weapon is not None:
                return weapon
    return weapondata.get(ammo)


def note_peer_fire(conn, body, room=None):
    """记一发 `rpFire`（§92）。解不出武器就不记。

    ★ 这条路上**不只有真人的子弹**：闯关里怪的枪也是控制者那台机器替它发的
    （`body+0 == 20`，§23）。两种都要记 —— 前者是「谁在打我」，
    后者既是威胁、又是**服务端唯一能看见怪在哪的时刻**（§125）。
    """
    if len(body) < botsync.FIRE_BODY_SIZE:
        return
    source = body[0]
    ammo, fx, fy, angle, power = struct.unpack_from("<iffff", body, 2)
    if source in MOB_FIRE_SOURCES:
        # ★★ 位置采样**排在武器解析前面**（§141）：怪 / boss 的枪一响，
        #    枪口坐标就是它此刻的位置 —— 就算这把枪不在武器表里
        #    （认不出弹速、躲不了它），这条坐标流也不能丢。
        note_mob_gun_muzzle(room, fx, fy)
    weapon = _quest_weapon(room, ammo)
    if weapon is None:
        return
    shots = getattr(conn, "peer_shots", None)
    if not isinstance(shots, collections.deque):
        shots = conn.peer_shots = collections.deque(
            maxlen=BOT_PEER_SHOT_KEEP)
    shots.append(PeerShot(weapon, fx, fy,
                          ballistics.launch(weapon, angle, power),
                          at=_now(), source=source))
    if source in MOB_FIRE_SOURCES:
        # ★ 怪开的枪：**不要**把这把枪记成「这个真人现在用的枪」——
        #   那会让 M5-C 的战力对比按怪的枪算。
        #   ⚠ 位置不用从这儿取：`rpAiMsg` 已经在广播它了（§125）。
        return
    # ★ 顺手记住「他现在用的是哪把枪」（M5-C 的战力对比要用）。
    #   `rpChangeWeapon` 也会写这一格，见 `note_peer_hit()`。
    conn.peer_weapon = weapon


def _peer_shot_velocity(conn, bx, by):
    """真人这一发**直接命中**时弹体的速度矢量；配不上返回 `None`（§92）。

    包里没有速度，也没有「这一发是哪一枪打出来的」——
    但 `rpFire` 给了发射点 / 角度 / 力度，闭式解一算就知道这条弹道**什么
    时候会走到爆点那一列**，走到时的速度是多少：

        t  = (爆点x − 发射点x) / v.x           ; v.x 是常数
        y  = 发射点y + t·v.y + g·t(t+1)/2      ; 和 `ballistics.position_at` 同一式
        v  = (v.x, v.y + g·t)                  ; 和 `_shell_velocity()` 同一式

    ⇒ 拿**预测的 y 和爆点 y 差多少**当匹配分，最接近的那一发就是它。
    差得超过 `BOT_PEER_SHOT_TOLERANCE` 就当没配上。

    ⚠ 配不上的已知情形：弹跳过的弹体（苹果雷撞地形会弹，§84 之后闭式解
    不成立）、追踪弹拐过弯的（§77）。这两种只是**少一份击退**，
    伤害和位置照旧 —— 不会把 bot 甩到别处去。
    """
    matched = _match_peer_shot(conn, bx, by)
    return None if matched is None else (matched[1], matched[2])


def _match_peer_shot(conn, bx, by):
    """爆点 `(bx, by)` 是**哪一发** `rpFire`；配不上返回 `None`。

    返回 `(PeerShot 记录, v.x, v.y)`。击退要的是速度，破坏物要的是那把枪
    （溅射半径），两边问的是同一件事，所以只有这一份匹配。
    """
    shots = getattr(conn, "peer_shots", None)
    if not shots:
        return None
    best = None
    for record in shots:
        shot = record.shot
        vx = shot.speed * math.cos(shot.angle)
        vy = shot.speed * math.sin(shot.angle)
        if abs(vx) < 1e-6:
            continue
        ticks = (bx - record.x) / vx
        if ticks < 0.0 or ticks > BOT_PEER_SHOT_TICKS:
            continue
        y = record.y + ticks * vy + shot.gravity * ticks * (ticks + 1.0) / 2.0
        error = abs(y - by)
        if best is None or error < best[0]:
            best = (error, vx, vy + shot.gravity * ticks, record)
    if best is None or best[0] > BOT_PEER_SHOT_TOLERANCE:
        return None
    return (best[3], best[1], best[2])


def _note_peer_breakable(room, handle, damage):
    """真人报过来的这一发是不是打在**可破坏物**上（§139）。是就照它扣。

    ★ 一个数都不用算：`rpSplashDamaged +8` 就是他那台机器**已经扣掉**的
      伤害，`+4` 是破坏物的世界句柄（产物里那一格 `handle`）。
      不记这本账的话，真人把冰砸开自己走过去了，bot 眼里那条路还堵着。
    """
    ledger = _breakables(room)
    if ledger is None or damage <= 0:
        return False
    terrain = mapdata.load(_current_map(room))
    if terrain is None or not terrain.breakables:
        return False
    got = ledger.apply_broadcast(terrain, handle, damage)
    if got is None:
        return False
    item, broke = got
    if broke:
        asynclog.emit(f"[{gameserver.ts()}]    破坏物碎了（真人打的）: 句柄 "
                      f"{item.handle} @ ({item.x}, {item.y})　"
                      f"{item.regen_ms / 1000.0:.0f} 秒后长回来")
    return True


def note_peer_hit(room, conn, payload):
    """真人发来的一发同步包 —— 打到 bot 身上就替它挨这一下击退（§92）。

    挂在 `gameserver.BOT_PEER_HIT` 上，`forward_peer_data()` 每发都问一次。
    **只管击退**：伤害是收方自己扣的（`rpExplode +24` / `rpSplashDamaged +8`
    原样进 `Character::OnHit`，§42），服务端不重算，bot 的血也不在这边记。

    两条路各取各的来向：

    * `rpExplode`（直接命中）—— 包里**没有**击退矢量，每台机器都拿自己那颗
      弹体的速度现算（`0x49285c`）。服务端只好按 `rpFire` 反推（见
      `_peer_shot_velocity()`）。
    * `rpSplashDamaged`（溅射 / 地面燃烧 / 近身）—— 击退矢量**就在包里**
      （`+13/+17`），照抄就行，一点都不用猜。
    """
    opcode = udpsync.peer_opcode(payload)
    if opcode == botsync.OP_FIRE:
        note_peer_fire(conn, payload[udpsync.PEER_HEADER_SIZE:], room)
        return
    if opcode == botsync.OP_AI_MSG:
        # ★★★ 怪的位置广播（§125）。只有控制者发，而控制者一定是真人（§22）。
        parsed = botsync.parse_ai_message(payload[udpsync.PEER_HEADER_SIZE:])
        if parsed is not None:
            note_ai_message(room, parsed[0], parsed[1])
        return
    if opcode == botsync.OP_CHANGE_WEAPON:
        # ★ 「他换枪了」——`rpChangeWeapon` body `+1..4` 就是 ammo id
        #   （`botsync.change_weapon_body()` 组的是同一份布局）。
        #   M5-C 拿它估对方的输出，估不出来时退回角色的缺省枪。
        body = payload[udpsync.PEER_HEADER_SIZE:]
        if len(body) >= 5:
            weapon = weapondata.get(struct.unpack_from("<i", body, 1)[0])
            if weapon is not None:
                conn.peer_weapon = weapon
        return
    if opcode not in (botsync.OP_EXPLODE, botsync.OP_SPLASH_DAMAGED):
        return
    body = payload[udpsync.PEER_HEADER_SIZE:]
    if opcode == botsync.OP_EXPLODE:
        if len(body) < botsync.EXPLODE_BODY_SIZE:
            return
        _handle, target, bx, by, _kind, _flags, damage = struct.unpack_from(
            "<iiffiif", body, 0)
        if target <= 0 or damage <= 0:
            return
        source = "真人直接命中"
        # ★★ 血量台账**排在击退前面**（M5-C）：这一发扣了多少血是包里写死的
        #   事实（§42 收方原样扣），和「配不配得上某一发 rpFire」无关。
        if botsync.handle_seat(target) is None:
            # ★★★★★ **先问「是不是可破坏物」**（V0.3 §165）——`rpSplashDamaged`
            #   那一路早就这么分流了，`rpExplode` 这一路一直漏着。
            #   实机 Esperan03 01:07~01:11 那一局：真人 13 发 `rpExplode`
            #   里有 **12 发**打的是破坏物（句柄 114/84/89/79/85，各 60 点），
            #   而整局 `破坏物碎了（真人打的）` **一行都没有** ⇒ 真人把底下
            #   那排台子打碎了，服务端这本账上它们还好好的，bot 照旧从
            #   碎掉的台子上**悬空走过去**（用户 2026-09-02 报的第三件）。
            #   ★ 直接命中也能打碎它：客户端自己就按这一发扣血，紧接着
            #     还发了 `0x0408 HP 归零上报 句柄=0x72`，服务端也广播了
            #     `0x0406` —— 两边都知道，只有 bot 的地形没跟上。
            if _note_peer_breakable(room, target, damage):
                return
            # ★ 打中的不是玩家座位、也不是破坏物 ⇒ 爆点是那只怪的一次额外
            #   位置采样（§125）。`create=True`：boss 从不广播坐标，直接命中
            #   是它唯一的位置来源（§141）。
            note_mob_hit(room, target, bx, by, create=True)
        _note_damage(room, botsync.handle_seat(target), damage)
        velocity = _peer_shot_velocity(conn, bx, by)
        if velocity is None:
            # ★ 诊断：配不上开火记录 = **这一发不给击退**（§92 的取舍）。
            #   用户报「有时候有击退，有时候没有」时，这一行是第一嫌疑。
            _note_no_knockback(room, botsync.handle_seat(target),
                               conn, source, damage, bx, by)
            return
        push = knockback_vector(velocity[0], velocity[1], damage)
    else:
        if len(body) < botsync.SPLASH_BODY_SIZE:
            return
        _source, target, damage, _z, push_x, push_y = struct.unpack_from(
            "<iifBff", body, 0)
        if target <= 0 or damage <= 0:
            return
        source = "真人溅射/火/近身"
        push = (push_x, push_y)
        if botsync.handle_seat(target) is None:
            hit_x, hit_y = struct.unpack_from("<ff", body, 21)
            # ★★★ 先问「是不是**可破坏物**」（§139）：原版的破坏物伤害走的
            #   就是这一发，`+4` 填的是它的世界句柄。不先分流的话它会被
            #   当成怪，喂进怪物表里一个根本不存在的位置。
            if _note_peer_breakable(room, target, damage):
                return
            note_mob_hit(room, target, hit_x, hit_y, create=True)
        _note_damage(room, botsync.handle_seat(target), damage)
    seat_index = botsync.handle_seat(target)
    if seat_index is None:
        return
    _knock_back_seat(room, seat_index, damage, push, source=source)


def _note_no_knockback(room, seat_index, shooter, source, damage, bx, by):
    """★ 诊断：这一发**没给击退**，把原因留一行（按分类去重，§94）。"""
    if seat_index is None:
        return
    seat = room.seats[seat_index] if 0 <= seat_index < len(room.seats) else None
    machine = None if seat is None else seat.conn
    if not isinstance(machine, BotConn):
        return
    key = (source, "配不上开火记录")
    if key in machine.knock_logged:
        return
    machine.knock_logged.add(key)
    shots = getattr(shooter, "peer_shots", None)
    machine.log(
        f"   挨打击退[{source}]: ★**没给击退** —— 爆点 ({bx:.0f}, {by:.0f}) "
        f"配不上真人的任何一发 `rpFire`（伤害 {damage:g}；账上有 "
        f"{0 if not shots else len(shots)} 发在飞）。已知配不上的两种："
        f"弹跳过的弹体（§84）和拐过弯的追踪弹（§77）")


def _knock_back_seat(room, seat_index, damage, push, source="?"):
    """把一次击退**结算到 bot 自己的身体上**（`0x50f7ca` 的 `kind == 0` 那一路）。

    `source` 只进日志（`_log_knockback`），不参与任何判定。

    座位上不是 bot、或者它还没落脚点，就什么都不做 —— 真人的击退归他自己
    那台机器算（那边收到 `rpExplode` / `rpSplashDamaged` 就会做，§92）。

    ## 为什么非做不可

    bot 的位置是**服务端说了算**的：每台客户端都把 bot 的角色硬同步到心跳
    报的坐标上（§5.6）。挨打时客户端各自把 bot 的模型顶飞了，而服务端这边
    的模型没动 ⇒ 下一发心跳（8 Hz）当场把它拽回去。
    用户 2026-08-28 报的「我打 bot，bot 不会被击退，只是原地跳一下」
    就是这个拽回去的过程。

    ## 原版怎么分档（`0x50f7ca`，`kind` 恒 0）

    开头那三条 `cmp` 先挑一个档位（`0x50f7d5` ~ `0x50f7ea`）：

        伤害 >= 10                -> 甲档
        伤害 <  10 且**腾空中**   -> 甲档
        伤害 <  10 且**在地上**   -> 乙档

    甲档（`0x50f864`）：

        伤害 **> 10** -> `[char+0x120] += push`
        伤害 <= 10    -> ★ 只有「在地上 **且** v.x == 0」才把 v.y 夹到 −10
                         （`0x50f884` 那两道门）—— 腾空那一批就此**什么都不做**
        ★ 出口无论如何都落到 `0x50f947`，把「我踩在地上」那一位清掉

    乙档（`0x50f849`）：只沿地面滑 `push.x × 3`（`0x50d9a7`），不离地。
    """
    seat = room.seats[seat_index] if 0 <= seat_index < len(room.seats) else None
    machine = None if seat is None else seat.conn
    if not isinstance(machine, BotConn) or machine.body is None:
        return
    body = machine.body
    damage = int(damage)
    if damage >= KNOCKBACK_MIN_DAMAGE or not body.on_ground:
        vx, vy = body.vx, body.vy
        if damage > KNOCKBACK_MIN_DAMAGE:
            vx += push[0]
            vy += push[1]
        elif body.on_ground and body.vx == 0.0:
            # 正好 10 点：不给速度，只把 v.y 夹到 −10（`0x50f8a2`）。
            vy = min(vy, KNOCKBACK_MIN_LIFT)
        # ★ 甲档一定离地：`0x50f8c3` 那条尾巴无论 v.y 正负都落到
        #   `0x50f947`，把「我踩在地上」那一位清掉。
        machine.body = botmove.Body(body.x, body.y, vx, vy, on_ground=False)
        # ★ 日志里把「push 到底给没给」写清楚 —— 伤害正好 10 的那一发
        #   原版只夹 `v.y`、一点 push 都不给（`0x50f864` 的 `jle`），
        #   不标出来的话日志上写着 push 却看不见位移，下次又要查一轮。
        _log_knockback(room, machine, source, damage, push, body,
                       "甲档" if damage > KNOCKBACK_MIN_DAMAGE
                       else "甲档·只夹v.y不给push")
        return
    # 乙档 · 在地上：横向滑一段，**不离地**。和火团往外铺是同一个例程
    #   （`0x50d9a7`），所以这里也用同一个模型：那一列上够得着的站立面。
    terrain = _terrain(room)
    if terrain is None:
        return
    span = push[0] * KNOCKBACK_SLIDE
    x = body.x + span
    # 够得着的坡度和走路同一条（`botmove.CLIMB_SLOPE`）—— 收方那边这一段
    # 走的就是走路那个例程。
    surface = botmove.surface_near(terrain, x, body.y,
                                   abs(span) * botmove.CLIMB_SLOPE)
    if surface is None:
        _log_knockback(room, machine, source, damage, push, body,
                       "乙档", note="滑不过去（那一列没有够得着的站立面）")
        return
    machine.body = botmove.Body(x, float(surface), on_ground=True)
    _log_knockback(room, machine, source, damage, push, body, "乙档")


#: 预演击退落点时最多推几个 tick（**只给日志用**，不影响任何判定）。
#: 上界来自 `2v/g`：最强那一档 push 是 40，`2 × 40 / 1.2 = 67` 个 tick，
#: 再宽一倍留给「被顶下悬崖一路往下掉」的情形。
KNOCKBACK_PREVIEW_TICKS = 200

#: 击退日志的**结果分类**（每个来源每一类只打第一发）。
#:
#: ★ 它同时是「还要不要预演」的判据：一个来源的这几类全打过了，
#: 后面同来源的击退连预演都不跑 —— 那一段推 tick 挂在**转发真人同步包**
#: 的路径上，不能让它一直烧 CPU（D1 / §182 的口径）。
KNOCK_KINDS = ("飞出去", "推了一小段", "★几乎没动", "★横向被地形挡住了",
               "滑不过去（那一列没有够得着的站立面）")


def _log_knockback(room, machine, source, damage, push, before, tier,
                   note=None):
    """★ 诊断：把这一下击退**推到落地**，一行讲清「飞了多远」（§94）。

    用户 2026-08-28 报「有时候有击退，有时候没有」——「有没有」这件事
    服务端手里本来就有答案（`machine.body` 前后一比），只是以前一个字都不打，
    日志里查不到。

    ★ 按**结果分类**去重（铁律 10 的口径）：key = `(来源, 分类)`，
    一张图里每一类只打第一发，不刷屏。真正想看的「这一类为什么没动」
    第一次发生时就留下了完整数字。
    """
    logged = machine.knock_logged
    if all((source, kind) in logged for kind in KNOCK_KINDS):
        return                          # 这个来源该说的都说过了，别再预演
    after = machine.body
    terrain = _terrain(room)
    landed, ticks, walled = after, 0, False
    if terrain is not None and not after.on_ground:
        who = _character_of(machine)
        while not landed.on_ground and ticks < KNOCKBACK_PREVIEW_TICKS:
            step = botmove.tick(terrain, landed, who)
            # ★ 「有水平速度，但这一 tick 一步都没挪」= 被地形挡住了
            #   （§95 之后速度不再被清零，所以判据从「速度归零」改成
            #   「位置没动」）。「飞起来了却横着没动」就是它，单独标出来。
            if step.vx and step.x == landed.x and not step.on_ground:
                walled = True
            landed = step
            ticks += 1
    span = landed.x - before.x
    if note is not None:
        kind = note
    elif walled and abs(span) < 60.0:
        kind = "★横向被地形挡住了"
    elif abs(span) >= 60.0:
        kind = "飞出去"
    elif abs(span) >= 12.0:
        kind = "推了一小段"
    else:
        kind = "★几乎没动"
    key = (source, kind)
    if key in machine.knock_logged:
        return
    machine.knock_logged.add(key)
    machine.log(
        f"   挨打击退[{source}/{tier}]: 伤害 {damage} 强度 "
        f"{knockback_strength(damage):g} push=({push[0]:.1f}, {push[1]:.1f})"
        f"；({before.x:.0f}, {before.y:.0f}) -> ({landed.x:.0f}, "
        f"{landed.y:.0f}) 位移 {span:+.0f} 滞空 {ticks} tick —— {kind}")


def _seat_group(room, seat_index):
    """这个座位的**碰撞排除组**（`rpFire body+1`，§63）。

    组队 / 闯关房是队伍号（1 / 2），个人战是座位 + 1。收方拿它决定
    「这颗子弹撞不撞这个人」—— **相同就整个跳过碰撞**，所以它同时也是
    服务端这边判命中的口径（两边必须是同一套，否则「客户端看着穿过去了、
    服务端说打中了」）。
    """
    seat = room.seats[seat_index] if 0 <= seat_index < len(room.seats) else None
    team = 0 if seat is None else seat.team
    return botsync.fire_group(seat_index, team)


def _seat_body(room, seat_index):
    """这个座位此刻的**落脚点 + 姿势**：`(x, y, 蹲着没有)`；不知道返回 `None`。

    真人的位置来自心跳轨迹（`sync_trail[-1]`），bot 的来自它自己的
    `battle_pos` —— 两边都要，因为判命中时**别的 bot 也挡子弹**。
    """
    seat = room.seats[seat_index] if 0 <= seat_index < len(room.seats) else None
    conn = None if seat is None else seat.conn
    if conn is None:
        return None
    if getattr(seat, "is_bot", False):
        position = getattr(conn, "battle_pos", None)
        if position is None:
            return None
        return (position[0], position[1], bool(getattr(conn, "crouched", False)))
    # ★★ **逐格外推出来的那份**（D106 / `_advance_humans`）：收方对远端
    #    角色也是每 32 ms 自己走一步的（§39），判命中要用同一个口径。
    #    还没外推过（没地形 / 刚进场）就退回轨迹最后那一点。
    body = getattr(conn, "sim_body", None)
    trail = getattr(conn, "sync_trail", None)
    if not trail:
        return None
    point = trail[-1]
    crouched = bool(point[7]) if len(point) > 7 else False
    if body is not None:
        return (body.x, body.y, crouched)
    return (point[0], point[1], crouched)


def _human_direction(keys):
    """按键掩码 -> 走路方向（`+1` 右 / `-1` 左 / `0` 站着），同 §39 的口径。"""
    right = bool(keys & botsync.KEY_RIGHT)
    left = bool(keys & botsync.KEY_LEFT)
    if right == left:
        return 0                        # 都没按 / 都按着 = 不走
    return 1 if right else -1


def _advance_humans(room, terrain):
    """把每个**真人**座位的身体往前推一格（D106）。

    ## 为什么要推

    收方对远端角色就是这么干的：`0x507660` 拿心跳里的**按键掩码**替它走，
    心跳只是每 128 ms 纠一次偏（§39）。服务端替 bot 判命中时用的「人在哪」
    必须是同一个口径 —— 拿 128 ms 前那一发心跳的坐标去撞此刻的弹体，
    跳起来 / 被顶飞的那几发根本判不准。

    旧 §96 是**事后插值**（拿这一帧和上一帧插出中间那几 tick）。它算得准，
    但要**等下一发心跳到了**才算得出来 —— 而 `rpExplode` 迟到一格就被收方
    静默丢弃、句柄账从此永久错开（§147）。所以 D106 换成逐格外推：
    用的全是**已经收到**的事实（最后一发心跳的位置 / 速度 / 按键），
    不是预测未来（铁律 10），而且下一发心跳一到就**硬置**回去，误差不累积。

    ★ 拿不到地形就什么都不做：那时 `_seat_body()` 退回轨迹最后那一点，
      和 D106 之前一样。
    """
    for index, seat in enumerate(room.seats):
        if seat is None or getattr(seat, "is_bot", False):
            continue
        conn = seat.conn
        if conn is None:
            continue
        trail = getattr(conn, "sync_trail", None)
        if not trail:
            continue
        point = trail[-1]
        mark = getattr(conn, "sync_trail_seq", 0)
        body = getattr(conn, "sim_body", None)
        if body is None or getattr(conn, "sim_body_mark", None) != mark:
            # ★ **硬置**：这一发心跳说的位置 / 速度 / 踩没踩地就是事实，
            #   外推出来的那点误差到此为止（和收方 `0x504215` 同一个道理）。
            conn.sim_body = botmove.Body(
                point[0], point[1], vx=point[4], vy=point[5],
                on_ground=bool(point[3]))
            conn.sim_body_mark = mark
            continue
        if terrain is None:
            continue
        who = chrprops.get(seat.character_id)
        keys = point[8] if len(point) > 8 else 0
        conn.sim_body = botmove.tick(
            terrain, body, who,
            direction=_human_direction(keys),
            fast_run=bool(point[6]), crouched=bool(point[7]))


def _seat_on_ground(room, seat_index):
    """这个座位此刻**踩没踩在地上**（`[char+0x128]`）；不知道返回 `None`。

    这一格就是心跳位域的 **bit2**（packet_api §5.5 / §5.6，V0.3 §35）：
    真人的从轨迹点上取（`SyncTrailPoint[3]`，收方写进 `[char+0x128]` 的
    正是它），bot 的从它自己上一帧报出去的那一位取。

    ★ 谁读它：`_direct_hit_damage()`（夺分模式的那条 ×0.75，§89）。
    """
    seat = room.seats[seat_index] if 0 <= seat_index < len(room.seats) else None
    conn = None if seat is None else seat.conn
    if conn is None:
        return None
    if getattr(seat, "is_bot", False):
        return getattr(conn, "on_ground", None)
    # ★ 先看轨迹：一条都没有 = 这个人报过位置没有我们**根本不知道**，
    #   外推出来的那份也就无从谈起（`sim_body` 是从轨迹硬置出来的）。
    trail = getattr(conn, "sync_trail", None)
    if not trail:
        return None
    body = getattr(conn, "sim_body", None)
    if body is not None:
        return bool(body.on_ground)
    point = trail[-1]
    return bool(point[3]) if len(point) > 3 else None


# ---------------------------------------------------------------------------
# ★★★ 血量台账（M5-C）—— 每台客户端本来就在记的那一份，服务端补一份
# ---------------------------------------------------------------------------
def _health(room):
    """这一局的血量台账（`bothp.Ledger`）；还没开局返回 `None`。

    ★ 懒挂在 `RoomQuest` 上：它跟着「一局」生灭，回房间时整个丢掉，
    和 `items_at` / `reflect_until` 那些同一个生命周期。放在这里而不是
    `gameserver` 里定义，是因为它**只有 bot 用得上** —— 真人那份账在
    他自己的客户端里（§42）。
    """
    quest = None if room is None else room.quest
    if quest is None:
        return None
    ledger = getattr(quest, "bot_health", None)
    if ledger is None:
        ledger = quest.bot_health = bothp.Ledger()
    return ledger


def _note_damage(room, seat_index, amount):
    """记一发打在某个座位身上的伤害（谁打的都记）。"""
    ledger = _health(room)
    if ledger is not None and seat_index is not None and 0 <= seat_index:
        ledger.note_damage(seat_index, amount)


def _seat_max_hp(room, seat_index):
    """这个座位的满血值（角色属性，`ChrProps.ini` 的 `Hp`）。"""
    seat = room.seats[seat_index] if 0 <= seat_index < len(room.seats) else None
    return chrprops.get(0 if seat is None else seat.character_id).hp


def _seat_health(room, seat_index):
    """这个座位还剩几成血（0.0 ~ 1.0）。没有台账就当满血。"""
    ledger = _health(room)
    if ledger is None:
        return 1.0
    return ledger.fraction(seat_index, _seat_max_hp(room, seat_index))


def _refresh_health(room):
    """每帧一次：认出「躺着 -> 站起来」的翻转，把那个座位的账清零。

    ★ 判据是**状态翻转**（铁律 10），不是「死后 5 秒」那种定时器 ——
    重生的真实时刻由 `respawn_due` 说了算，看门狗撤闩那一下就是它。
    """
    ledger = _health(room)
    if ledger is None:
        return
    for index, seat in enumerate(room.seats):
        if seat is None:
            continue
        if ledger.note_lying(index, _lying_dead(room, index)):
            ledger.reset(index)
    _advance_hp_charges(room, ledger)


def _advance_hp_charges(room, ledger):
    """把 HP 回复剂那 8 跳按原版节奏加进台账（`Status.ini[8]`，§122）。

    ★ 这里的「每 1 秒一跳」**是原版数据**（`Interval=1.0`），不是我们挑的
    定时器 —— 铁律 10 禁的是拿观测值当阈值，照抄原版节奏不在此列。
    """
    charges = getattr(room.quest, "hp_charges", None)
    if not charges:
        return
    now = _now()
    for seat in list(charges):
        entry = charges[seat]
        while entry[1] > 0 and now >= entry[0]:
            ledger.note_heal(seat, gameserver.HP_CHARGE_AMOUNT)
            entry[0] += gameserver.HP_CHARGE_INTERVAL
            entry[1] -= 1
        if entry[1] <= 0:
            charges.pop(seat, None)


def _seat_velocity(room, seat_index):
    """这个座位此刻的速度（**单位 / tick**）；量不出来返回 `(0, 0)`。

    真人从心跳轨迹上量（相邻两点差一发心跳 = 4 个 tick，§71），
    bot 直接读它自己的 `Body` —— 那本来就是同一个量纲。
    """
    seat = room.seats[seat_index] if 0 <= seat_index < len(room.seats) else None
    conn = None if seat is None else seat.conn
    if conn is None:
        return (0.0, 0.0)
    if getattr(seat, "is_bot", False):
        body = getattr(conn, "body", None)
        if body is None:
            return (0.0, 0.0)
        if body.on_ground:
            # ★ 踩在地上时 `Body` 的速度恒为 0（§35 的口径），可人确实在走
            #   —— 拿上一帧报出去的位置差补上，和真人那条路同一个量纲。
            last = getattr(conn, "battle_pos_prev", None)
            if last is None:
                return (0.0, 0.0)
            return botaim.sample_velocity([last, (body.x, body.y)])
        return (body.vx, body.vy)
    trail = getattr(conn, "sync_trail", None)
    if not trail or len(trail) < 2:
        return (0.0, 0.0)
    return botaim.sample_velocity([trail[-2], trail[-1]])


#: ★ **道具自己那个碰撞圆的半径**（V0.3 §100）。
#:
#: 判定本身是逆出来的：`Character::CheckItemPickup`（`0x5154d3`）扫世界
#: 第 2 类（掉落物），对每一件先看 `[item+0x2a8] == 0`（还没被捡）和
#: `[item+0x2aa] != 0`（可捡），再调 **`0x50f410`** —— 和「子弹撞人」
#: 「弹跳台弹人」是**同一个**圆相交函数。
#:
#: ★★ **18 是逆出来的，不再是估的**（V0.3 §118）：`Item` 构造函数
#: `0x51f2e2` 那一句 `mov [[item+0x13c]+0x18], 0x41900000` 写死的就是
#: **18.0f** —— 那一格正是碰撞形状的半径，`0x50f410` 拿它和角色那三个圆求交。
#: （§100 当时按弹跳台那个 20.0 取的同量级值，偏大 2。）
BOT_ITEM_RADIUS = 18.0


def _item_pickups(room, machine, seat_index):
    """bot 踩到地上的道具就捡起来（V0.3 §100）。返回捡到的 `[(句柄, 物件id)]`。

    ## 为什么非服务端做不可

    拾取这条链原本**整条都是客户端发起**的：`CheckItemPickup` 在**每台机器
    自己的本地玩家**身上跑，踩到了就发 `0x0407` 问服务端「能捡吗」。
    bot 没有本机 ⇒ 一辈子发不出那一发 ⇒ **一件都捡不到**
    （用户 2026-08-29 报的就是这个）。

    ## 位置从哪来

    老口径下服务端**不知道道具在哪**（随便给个大坐标、让客户端取模+顶出
    地形，§192）。V0.3 §100 把刷新点改成按地形挑的合法落点并记进
    `quest.items_at`，这里才判得了「踩到没有」。

    ## 捡到之后

    * 广播 `0x0405` —— 每台机器把这件东西从世界里抹掉、放拾取特效；
    * **绝不发 `0x040b`**：那一发是「往**收包的那个人**的道具槽里塞一件」，
      bot 没有客户端，发给谁都是凭空多给别人一件。bot 的道具只记在服务端
      那份镜像（`quest.grant_item`）里。
    """
    quest = room.quest
    body = machine.body
    if quest is None or body is None:
        return []
    # ★★ 先把**玩家屏幕上已经没了**的那几件摘掉（§118）：原版的掉落物
    #    13 秒就自己消失，不摘的话 bot 会去捡一件谁也看不见的东西。
    quest.expire_items(_now())
    if not getattr(quest, "items_at", None):
        return []
    character = chrprops.get(machine.character_id)
    circles = character.circles(body.x, body.y, machine.crouched)
    got = []
    for handle, spot in list(quest.items_at.items()):
        ix, iy = spot
        if not any(math.hypot(ix - cx, iy - cy) <= r + BOT_ITEM_RADIUS
                   for cx, cy, r, _region in circles):
            continue
        if not quest.claim_item(handle, seat_index):
            continue
        item_id = quest.item_id_of(handle)
        machine.battle_broadcast(
            gameserver.build_game(
                gameserver.OP_PICKED_ITEM,
                gameserver.build_picked_item(seat_index, handle)),
            reason=f"：bot 捡到 {item_id}")
        got.append((handle, item_id))
        if item_id in gameserver.GRANTABLE_ITEM_IDS:
            quest.grant_item(seat_index, item_id)
            held = quest.item_slots[seat_index]
            machine.log(
                f"   捡到道具 {item_id} "
                f"{gameserver.ITEM_NAMES.get(item_id, '未知物件')} "
                f"@ ({ix:.0f}, {iy:.0f})；手上 {len(held)} 件 {list(held)}")
        elif item_id in gameserver.PVP_WEAPON_ITEM_IDS:
            _take_weapon_item(machine, seat_index, item_id)
    return got


def _take_weapon_item(machine, seat_index, item_id):
    """捡到地上那三把特殊武器（§223）—— bot 得**服务端自己换枪**。

    真人是客户端在收到 `0x0405` 的那一刻自己调 `vf_11c` 换的，而那一句里
    有 `cmp [char+0x2ac], 0x409f7d()`：**只有那台机器上的本地玩家会换**。
    bot 不是任何一台机器的本地玩家 ⇒ 没有一台会替它换 ⇒ 必须这边换完再
    用 `rpChangeWeapon` 声明出去。

    ★★ **两个额度也在这里起算**（V0.3 §115）：`vf_11c` 调的是
    `0x517121(角色, 武器id, 0)` 且 eax = 0 —— 那两个 0 的意思正是
    「用这把枪自己的 `ForceTime` / `ForceCount`」（`0x517130` / `0x517148`
    两处从武器记录的 `+0x94` / `+0x98` 补进来）。
    """
    weapon_id = gameserver.PVP_WEAPON_GIVES.get(item_id)
    weapon = None if weapon_id is None else weapondata.get(weapon_id)
    if weapon is None:
        machine.log(f"   捡到特殊武器 {item_id}，但服务端没有这把枪的记录，只捡不换")
        return
    now = _now()
    machine.item_weapon = weapon
    # 0 = 这把枪不限发数 / 不限时（原版拿 0 当「这一路不设限」，`0x48ba6e`
    # 和 `0x48ba82` 两处都是「参数为 0 就不写那一格」）。
    machine.item_weapon_shots = weapon.force_count or None
    machine.item_weapon_until = (now + weapon.force_ms / 1000.0
                                 if weapon.force_ms else None)
    machine.log(f"   捡到特殊武器 {item_id} -> {weapon.id}({weapon.section}) "
                f"伤害 {weapon.damage}；额度 "
                f"{_item_weapon_budget(machine)}")
    with contextlib.suppress(botsync.SyncInvariantError):
        _declare_weapon(machine, seat_index, machine.weapon)


def _item_weapon_budget(machine):
    """捡来那把枪的额度，给日志看的一行字。"""
    parts = []
    if machine.item_weapon_shots is not None:
        parts.append(f"{machine.item_weapon_shots} 发")
    if machine.item_weapon_until is not None:
        parts.append(f"{machine.item_weapon.force_ms / 1000.0:g} 秒")
    return " + ".join(parts) if parts else "无（下一帧就还原）"


def _spend_item_weapon_shot(machine):
    """打完一发，捡来那把枪的**次数**减一（原版 `0x48bade`）。

    ★ 只对「有发数限制」的那一把（核弹发射器）有效；限时的两把喷射器
    `ForceCount` 是 0，原版那一跳（`cmp [+0x2c], 0` / `[+0x34] > 0`）
    压根不会减到它们头上。
    """
    if machine.item_weapon is None or machine.item_weapon_shots is None:
        return
    if machine.item_weapon_shots > 0:
        machine.item_weapon_shots -= 1


def _expire_item_weapon(room, machine, seat_index, now):
    """捡来的那把枪打完 / 到点了就换回自己那把（V0.3 §115）。换了返回 `True`。

    ## 为什么非服务端做不可

    和 §100 的拾取、§109 的状态是**同一个形状**：真人那把临时枪的两个额度
    记在**他自己那台机器**上（`[持枪器+0x30/0x34/0x38]`），每帧由
    `0x48be09` 判「两个都到头了 ⇒ `[+0x2c] = −1`，换回本来那把」。
    bot 没有本机 ⇒ 没有一台会替它数 ⇒ 它会**一辈子举着那把枪**
    （用户 2026-08-29 报的就是这个）。而收方那边 `rpChangeWeapon` 走的是
    「直接改武器」那一路，**不带额度**，所以别人的机器也不会自己还原。

    ## 判据照抄 `0x48be09`

    ```text
    还能打的发数 = (ForceCount != 0) ? 剩余发数 : 0      ← 0x48ba3a
    还能拿的时间 = (到期时刻 != 0 && 到期 > 现在) ? … : 0 ← 0x48ba47
    两个都是 0  ⇒  换回去
    ```

    ⇒ **一把两样都没写的枪捡到手就会立刻还原**。原版就是这个结果，
    地上那三把各写了一样，所以谁也碰不到这条边。
    """
    if machine.item_weapon is None:
        return False
    shots_left = machine.item_weapon_shots or 0
    timed_out = (machine.item_weapon_until is None
                 or machine.item_weapon_until <= now)
    if shots_left > 0 or not timed_out:
        return False
    spent = machine.item_weapon
    machine.drop_item_weapon()
    own = machine.weapon
    machine.log(f"   捡来的 {spent.id}({spent.section}) 用完了，换回 "
                f"{own.id if own is not None else '（这个角色没有能用的枪）'}")
    # ★ `own is None` = 这个角色一把可用的枪都没有（D29）。这时**不发**
    #   `rpChangeWeapon` —— 没有 id 可填，而且 bot 本来也不会开火。
    if own is not None:
        _declare_weapon(machine, seat_index, own)
    return True


def _use_held_item(room, machine, seat_index):
    """手上有道具就用掉（V0.3 §100 / D65）。用了返回 `True`。

    发的是 `0x040a` 广播 —— 和真人按 Ctrl 之后服务端回的那一发一模一样，
    每台机器按包里的**座位号**找到 bot 的角色、调 `Character::UseItemEffect`。

    ⚠ **「什么时候用」原版没有答案**（那时候没有 bot），所以这一条是我们
    定的：**捡到就用**（D65）。理由是这 16 件里绝大多数是「立刻生效、持续
    一段时间」的增益，攒着没有任何机制上的好处，而 4 个格子占满之后再捡
    就直接丢了。要改成别的策略就改这一个函数。
    """
    quest = room.quest
    if quest is None:
        return False
    if not quest.item_slots or not 0 <= seat_index < len(quest.item_slots):
        return False
    held = quest.item_slots[seat_index]
    if not held:
        return False
    slot = _item_slot_to_use(room, seat_index, held)
    if slot is None:
        return False
    item_id = quest.use_item(seat_index, slot)
    if item_id is None:
        return False
    machine.log(f"   用道具 {item_id} "
                f"{gameserver.ITEM_NAMES.get(item_id, '未知物件')}"
                f"（捡到就用，D65）")
    machine.battle_broadcast(
        gameserver.build_game(gameserver.OP_ITEM_EFFECT,
                              gameserver.build_item_effect(seat_index, item_id)),
        reason=f"：bot 的道具 {item_id} 生效")
    # ★★ 「作用于别人」的那几件（反射 / 冰冻 / 烟雾 / 糊屏）服务端要自己记
    #    一份 —— 真人用的时候走的是 `on_use_item`，bot 走的是这里，
    #    漏掉的话 bot 放的烟雾罩不住别的 bot、放的糊屏也不影响谁（§121）。
    machine.note_area_item(item_id, seat_index, quest)
    # ★★★ 按发数算的那三条状态：服务端得**自己开始数**（§117）。
    #     有 `Time` 的客户端会自己撤，只有这三条要人补 `0x040d`。
    entry = gameserver.MAGAZINE_STATUS.get(item_id)
    if entry is not None:
        attr_id, rounds = entry[0], entry[1]
        machine.magazine_attrs[attr_id] = rounds
        machine.log(f"   状态 {gameserver.CHAR_ATTR_NAMES.get(attr_id, attr_id)}"
                    f"（属性 {attr_id}）挂上了，还能打 {rounds} 发")
    return True


#: 满血时**先不喝**的那几件（`Item.ini` 的两条 HP 回复剂）。
HOLD_WHEN_HEALTHY = (gameserver.HP_CHARGE_ITEM_ID,
                     gameserver.TEAM_HP_CHARGE_ITEM_ID)


def _item_slot_to_use(room, seat_index, held):
    """这一帧该用第几格的道具；一件都不该用返回 `None`。

    D65 定的是「捡到就用」，理由是绝大多数道具攒着没好处、而格子只有 4 个。
    **回血药是唯一的例外**：满血时喝掉就是白喝（`Status.ini[8]` 那 80 点
    全溢出），而它恰恰是残血时最想要的一件。所以：

    * 有别的道具 -> 先用别的（回血药留着）；
    * 只剩回血药 -> **掉了血才喝**；
    * 格子满了 -> 照喝不误 —— 不喝的话后面捡的全丢（`grant_item` 返回 False）。

    ⚠ 「掉了血」是**事实**（台账里有伤害），不是「低于三成」那种阈值。
    """
    healthy = _seat_health(room, seat_index) >= 1.0
    if not healthy:
        return 0
    for index, item_id in enumerate(held):
        if item_id not in HOLD_WHEN_HEALTHY:
            return index
    if len(held) >= gameserver.ITEM_SLOT_COUNT:
        return 0                          # 格子满了，再留着就浪费后面捡的
    return None


def _magazine_ratios(machine):
    """身上那些按发数算的状态叠出来的 `(伤害倍率, 弹体大小倍率)`（§117）。

    强力射击是 `DamageRatio=2.0` / `SizeRatio=2.0`，另外两条都是 1。
    ★ **`SizeRatio` 非做不可**：每台客户端都会把 bot 那颗弹体照着放大，
    服务端还按原半径判地形 / 判命中的话，两边的弹体又不是同一颗了
    （§116 刚修掉的正是这类分歧）。
    """
    damage = size = 1.0
    for attr_id in machine.magazine_attrs:
        for entry in gameserver.MAGAZINE_STATUS.values():
            if entry[0] == attr_id:
                damage *= entry[2]
                size *= entry[3]
                break
    return (damage, size)


def _spend_magazine_shots(room, machine, seat_index):
    """开了一发 —— 身上那些按发数算的状态各减一，数完的**撤掉**（§117）。

    原版是持有者那台机器数的：`Status.ini` 只写了 `Magazine`、没有 `Time`，
    `UseItemEffect` 给的时长是 −1，属性表每帧扫过去时靠这个计数收尾，
    收尾的那一下顺手发一发 `0x040d`（`Character::RemoveAttrEffect` 里
    `if ([char+0x2ac] == 我的座位)` 那一句，§200）。

    ⇒ **不发这一发，别人屏幕上那个效果永远不会结束**
    —— 用户 2026-08-29 报的「bot 的苹果弹一直是加强状态」就是它。
    """
    if not machine.magazine_attrs:
        return
    for attr_id in list(machine.magazine_attrs):
        left = machine.magazine_attrs[attr_id] - 1
        if left > 0:
            machine.magazine_attrs[attr_id] = left
            continue
        del machine.magazine_attrs[attr_id]
        name = gameserver.CHAR_ATTR_NAMES.get(attr_id, attr_id)
        machine.log(f"   状态 {name}（属性 {attr_id}）打完了，撤掉")
        try:
            machine.battle_broadcast(
                gameserver.build_game(
                    gameserver.OP_REMOVE_CHAR_ATTR,
                    gameserver.build_remove_char_attr(seat_index, attr_id)),
                reason=f"：bot 的状态 {name} 结束")
        except OSError as error:
            machine.log(f"   ⚠ 状态 {name} 的结束没广播出去（{error!r}），"
                        f"服务端这边照样算它结束了")


def _broadcast_status(room, machine, seat_index, status_item_id, why):
    """让**每台机器**都给 bot 挂上一个状态（V0.3 §109）。

    ## 为什么必须发这一发

    §101 的形状是「效果每台机器各算各的，而 bot 没有本机」。会话 34 的解法是
    「服务端自己也算一遍」—— 走位对了，可**别人屏幕上什么都看不见**：
    收方的角色是靠心跳里那六位方向键**自己走**的（§39），心跳只纠偏。
    服务端把 bot 的走速压到 0.3，客户端却照旧按满速走再被拽回来 ——
    看起来就是「一切正常」，用户报的「bot 走过胶水没有被粘住」就是这个。

    正解不是我们去猜别人该看见什么，而是**替 bot 走一遍原版那条链**：
    `Item.ini` 里状态本身就是四件道具（10600 中毒 / 10601 冻住 /
    10602 幽灵 / 10603 减速），客户端自己也是这么用的（冰冻在 `0x50886a`
    拿 10601 再调一次 `UseItemEffect`）。广播一发
    `0x040a gspItemEffect(bot 的座位, 10603)`，每台机器就按
    `Status.ini` 给 bot 加上「4 秒、走速 ×0.3」，模型、特效、本地走位一起对。

    效果的**结束**不用管：这四条都有 `Time`，客户端自己会到点撤掉
    （要靠 `0x040d` 撤的只有 `Magazine` 那一族，§200）。
    """
    try:
        machine.battle_broadcast(
            gameserver.build_game(
                gameserver.OP_ITEM_EFFECT,
                gameserver.build_item_effect(seat_index, status_item_id)),
            reason=f"：bot 身上的状态 {status_item_id}（{why}）")
    except OSError as error:
        machine.log(f"   ⚠ 状态 {status_item_id} 广播失败（{error!r}），"
                    f"服务端这边照样算")


def _live_slow_mines(quest, now):
    """此刻**真的还在地上、而且已经布好**的那几摊胶水（V0.3 §108）。

    到期的当场从表里摘掉 —— 客户端那边 `SlowMineObject::Tick` 也是自毁。
    """
    mines = getattr(quest, "slow_mines", None)
    if not mines:
        return ()
    live = []
    for mine in list(mines):
        born = mine[3] if len(mine) > 3 else None
        if born is None:
            live.append(mine)                      # 老格式（没记时刻）
            continue
        age = now - born
        if age >= gameserver.SLOW_MINE_LIFE_SECONDS:
            mines.remove(mine)                     # 15 秒到了，那摊没了
            continue
        if age < gameserver.SLOW_MINE_ARM_SECONDS:
            continue                               # 头 3 秒还没布好
        live.append(mine)
    return live


def _step_on_slow_mine(room, machine, seat_index, now):
    """bot 站在地上那摊减速胶水里就中招（V0.3 §101 / §105 / §108）。中了返回 `True`。

    真人是**自己那台机器**判的：胶水那一摊是个独立物件（id 10603 `Slowed`），
    谁碰到谁那边给谁挂 `Slowed`（`Status.ini` 第 14 条：4 秒、走速 × 0.3）。
    bot 没有本机 ⇒ 没有一台会替它算，而且就算别人那台把 bot 的模型减速了，
    下一发心跳（服务端算的）当场把它拽回去 —— 和 §92 的击退**同一个形状**。

    ⇒ 两件事一起做：服务端自己压走速（走位才对），**并且**广播一发
    `0x040a`（别人屏幕上才看得见，见 `_broadcast_status`）。

    ★ **不消耗那一摊**：胶水是留在地上的，站在里面就一直被减速
    （`slowed_until` 每帧往后推），但那一摊 15 秒之后自己没了（§108）。
    """
    quest = room.quest
    body = machine.body
    if quest is None or body is None:
        return False
    mines = _live_slow_mines(quest, now)
    if not mines:
        return False
    character = chrprops.get(machine.character_id)
    circles = character.circles(body.x, body.y, machine.crouched)
    reach = gameserver.SLOW_MINE_RADIUS
    for mine in mines:
        mx, my = mine[0], mine[1]
        if not any(math.hypot(mx - cx, my - cy) <= r + reach
                   for cx, cy, r, _region in circles):
            continue
        was = machine.slowed_until
        machine.slowed_until = now + gameserver.SLOWED_SECONDS
        if was is None or was <= now:
            machine.log(
                f"   踩进减速胶水 @ ({mx:.0f}, {my:.0f})：走速 × "
                f"{gameserver.SLOWED_SPEED_RATIO}，"
                f"{gameserver.SLOWED_SECONDS:g} 秒")
            _broadcast_status(room, machine, seat_index,
                              gameserver.STATUS_ITEM_SLOWED, "踩到胶水")
        return True
    return False


def _take_freeze(room, machine, seat_index, now):
    """别人放的冰冻把 bot 冻住（V0.3 §106）。冻上了返回 `True`。

    原版的判据整条都在 `UseItemEffect` 的 10310 分支（`0x5087b6`）里：

        不是自己（0x5087d1）
        [char+0x2b4] == 0（0x5087d9）
        **队伍号不同**（[vft+0x144]，0x5087e6）
        dist < Range（0x50884a；Range 来自 `Item.ini` 的 `[Freezer] Range=300`）

    冻多久看 `Status.ini` 第 12 条：`Time=2.0`。

    ★ 每一发冰冻只结算一次：结算完就把它从 `freeze_bursts` 里摘掉
    （那张表是**给 bot 用的待办**，不是场上的对象）。
    """
    quest = room.quest
    body = machine.body
    if quest is None or body is None or not getattr(quest, "freeze_bursts", None):
        return False
    my_seat = room.seats[seat_index] if 0 <= seat_index < len(room.seats) else None
    caught = False
    for burst in list(quest.freeze_bursts):
        bx, by, owner, _when = burst
        quest.freeze_bursts.remove(burst)
        if owner == seat_index:
            continue                                  # 不冻自己
        other = (room.seats[owner]
                 if 0 <= owner < len(room.seats) else None)
        if (my_seat is not None and other is not None
                and my_seat.team == other.team
                and room.team_layout() == lobby_module.TEAM_LAYOUT_TEAMS):
            continue                                  # 队伍号相同不冻
        if math.hypot(bx - body.x, by - body.y) >= gameserver.FREEZER_RANGE:
            continue
        machine.frozen_until = now + gameserver.FROZEN_SECONDS
        machine.log(f"   被冻住 @ ({bx:.0f}, {by:.0f})："
                    f"{gameserver.FROZEN_SECONDS:g} 秒不能动")
        # ★ 和胶水同一条路（§109）：不广播的话别人屏幕上 bot 照跑不误。
        _broadcast_status(room, machine, seat_index,
                          gameserver.STATUS_ITEM_FREEZED, "被冰冻")
        caught = True
    return caught


#: ★★ 被糊屏罩住时**瞄准失误概率**加多少（V0.3 §121）。
#:
#: 用户 2026-08-29：「别人使用了干扰道具，bot 的发射子弹的失误概率应该
#: **明显增加**。」中等难度 0.22 + 0.40 = 0.62 —— 三发里有将近两发歪，
#: 屏幕上看得出来「它被糊住了」。★ 没有原版出处（原版那条压根不生效，
#: §121），觉得太狠 / 太轻就改这一个数。
BOT_HUD_JAM_AIM_ERROR = 0.40


def _hud_jam_bonus(room, machine, seat_index):
    """这个座位此刻被糊屏罩着吗 —— 罩着就返回那一段失误加成，否则 0。"""
    quest = None if room is None else room.quest
    table = getattr(quest, "hud_jam_until", None) if quest is not None else None
    if not table:
        return 0.0
    until = table.get(seat_index)
    if until is None:
        return 0.0
    if _now() >= until:
        table.pop(seat_index, None)
        return 0.0
    return BOT_HUD_JAM_AIM_ERROR


def _in_smoke(room, x, y, now):
    """`(x, y)` 这个点在不在还没散的烟雾里（D67）。"""
    quest = room.quest
    smokes = getattr(quest, "smokes", None) if quest is not None else None
    if not smokes:
        return False
    for cloud in list(smokes):
        cx, cy, until = cloud
        if now >= until:
            smokes.remove(cloud)
            continue
        if math.hypot(cx - x, cy - y) <= gameserver.SMOKE_RADIUS:
            return True
    return False


def _smoke_cover(room, seat_index, now):
    """有没有敌人躲在烟里 —— 返回 `(云心x, 云心y, 那个人的座位)`；没有返回 `None`。

    ★ 这是 D67 那条「云里的人挑不中」的**另一半**：挑不中不等于不还手。
    用户 2026-08-29：「别人使用了烟雾道具，bot 应该模拟类似真人的无法判断
    敌人具体位置的样子，**只能向烟雾团整体乱射**。」
    ⇒ 挑不中具体的人，就朝云团里乱放 —— 真人就是这么打的。
    """
    quest = None if room is None else room.quest
    smokes = getattr(quest, "smokes", None) if quest is not None else None
    if not smokes:
        return None
    layout = room.team_layout()
    if layout == lobby_module.TEAM_LAYOUT_COOP:
        return None
    seat = room.seats[seat_index] if 0 <= seat_index < len(room.seats) else None
    my_team = None if seat is None else seat.team
    best = None
    for index, other in enumerate(room.seats):
        if other is None or index == seat_index:
            continue
        if layout == TEAM_LAYOUT_TEAMS and other.team == my_team:
            continue
        if _lying_dead(room, index):
            continue
        body = _seat_body(room, index)
        if body is None:
            continue
        for cloud in list(smokes):
            cx, cy, until = cloud
            if now >= until:
                smokes.remove(cloud)
                continue
            span = math.hypot(cx - body[0], cy - body[1])
            if span > gameserver.SMOKE_RADIUS:
                continue
            if best is None or span < best[0]:
                # ★ 云心抬到**身体那个圆**的高度：`smokes` 里记的是使用者的
                #   落脚点（脚下），照着脚打等于往地里放 —— 弹道会被地形挡住，
                #   于是「朝烟乱射」变成「站在烟前面发呆」。
                aim = _aim_point(room, index, cx, cy, False)
                best = (span, (aim[0], aim[1], index))
    return None if best is None else best[1]


def _smoke_aim(room, machine, seat_index, now):
    """朝云团里的一个**随机点**乱射；没有可打的烟返回 `None`。

    偏移量掷一次就存着，**打出一发之后**才重掷（`_reroll_aim_miss()`）——
    和瞄准失误同一个口径（D79）：逐帧重掷会让准星在云里乱抖。
    """
    cover = _smoke_cover(room, seat_index, now)
    if cover is None:
        machine.smoke_offset = None
        return None
    if machine.smoke_offset is None:
        # ★ 在云那个**圆盘**里均匀取一点（极坐标），不是在外接方框里取 ——
        #   方框会把落点扔到云外面去，最常见的后果是打进地里（弹道被地形挡住
        #   ⇒ 这一发干脆不打了，看着像 bot 在烟前面发呆）。
        angle = machine.roll(360) * math.pi / 180.0
        span = gameserver.SMOKE_RADIUS * machine.roll(1000) / 1000.0
        machine.smoke_offset = (span * math.cos(angle), span * math.sin(angle))
    return (cover[0] + machine.smoke_offset[0],
            cover[1] + machine.smoke_offset[1], int(cover[2]))


def _item_goal(room, machine, seat_index):
    """地上离自己最近的那件道具在哪；没有（或者不是道具局）返回 `None`。

    ★ 「捡道具」原本只在**踩到**的时候发生（§100），也就是说 bot 只能
    顺路蹭到。用户 2026-08-29 要的是「**主动**捡道具」，所以这里把它变成
    一个寻路目标 —— 走过去这件事本身仍然由 `botnav` 的真实物理决定
    （够不着的高台上那件自然就规划不出路线，也就不去了）。
    """
    quest = None if room is None else room.quest
    body = machine.body
    if quest is None or body is None:
        return None
    items = getattr(quest, "items_at", None)
    if not items:
        return None
    best = None
    for _handle, spot in items.items():
        span = math.hypot(spot[0] - body.x, spot[1] - body.y)
        if span > BOT_ENGAGE_RANGE:
            continue                      # 半张图外的东西不值得专门跑一趟
        if best is None or span < best[0]:
            best = (span, (float(spot[0]), float(spot[1])))
    return None if best is None else best


def _speed_scale(machine, now):
    """bot 这一帧的走速倍率（`Status.ini` 的 `SpeedRatio`）。

    冻住的时候是 **0** —— `Freezed` 那一条没有 `SpeedRatio`，它是整个
    「动不了」（`Status.ini` 第 12 条只有 `Time=2.0`）。
    """
    frozen = machine.frozen_until
    if frozen is not None:
        if now < frozen:
            return 0.0
        machine.frozen_until = None
    until = machine.slowed_until
    if until is None:
        return 1.0
    if now >= until:
        machine.slowed_until = None
        return 1.0
    return gameserver.SLOWED_SPEED_RATIO


def _battle_bodies(room, shooter_seat, group=None, include_self=False):
    """场上活着、位置已知的人：`[(座位号, x, y, 蹲着没有, 角色id)]`。

    `group` 给了就按**碰撞排除组**过滤（和收方一模一样，§63）：组号相同的
    跳过。**弹体**走这条 —— 队友因此天然被排除掉，个人战里则谁都撞得着，
    这正是原版「组队战没有直接友军伤害」的实现方式。

    ★★ `group=None` = **谁都算**（连自己）。**溅射和火墙走这条**（§69）：
    收方给溅射对象设组的那一句 `0x48254a` 外面套着
    `cmp byte [weapondef+0x54], 0`（= `SplashTeam`），而 **228 个武器节里
    一个都没填 `SplashTeam`** ⇒ 溅射对象的组恒为 0 = 撞所有人。
    语料也是这么说的：13160 发 `rpSplashDamaged` 里有 **1513 发是打到
    射手自己**的。用户 2026-08-27 报的就是这条：「组队战不能直接伤害友军没错，
    但溅射可以伤害友军」。

    ★★★ **但上面那两条只在对战模式成立** —— 闯关（任务）房里**一个角色
    都不返回**（§142，用户 2026-08-30 实机报的：「任务模式中 bot 的溅射和
    火墙会伤到队友，还弹出 bot1 击杀 bot2」）。任务模式里玩家造成的伤害
    **只落在怪和场景物上**，队友和射手自己一点都不吃：语料 8 个闯关局的
    「怪 AI 广播密集时段」里，玩家弹体的 3092 发 `rpSplashDamaged` +
    2034 发 `rpExplode` **无一发**的受害者是角色句柄（自己也没有），
    打到角色的 1337 发**全部**是怪的弹（源句柄在怪那一族，由控制者代发）。
    ⇒ 这道门放在这里而不是各个伤害点上：溅射 / 火墙 / 弹体 / 近身四条路
    的「碰得着谁」全问这一个函数，一处堵住就全堵住了（D102）。
    """
    if room.team_layout() == lobby_module.TEAM_LAYOUT_COOP:
        return []
    out = []
    for index, seat in enumerate(room.seats):
        if seat is None:
            continue
        if index == shooter_seat and not include_self:
            continue
        if group is not None and _seat_group(room, index) == group:
            continue
        if _lying_dead(room, index):
            continue
        body = _seat_body(room, index)
        if body is None:
            continue
        out.append((index, body[0], body[1], body[2], seat.character_id))
    return out


def _hostile_targets(room, seat_index):
    """这个 bot 该打谁 —— 返回 `[(座位号, x, y, 蹲着没有)]`。

    判据三条：**位置已知**、**活着**、**和它不同队**。

    ★★ **别的 bot 也算敌人**（用户 2026-08-27 报的）：以前这里只扫
    `room.human_seats()`，于是组队房里「真人 + bot 队友 vs 两个 bot 敌人」
    会变成 —— bot 队友那边一个敌对**真人**都没有（敌方全是 bot）⇒ 它一枪
    不放；敌方 bot 也看不见我方 bot ⇒ 它们只打真人。场上只有真人在挨打。
    原版里座位上坐的是谁跟「该不该打」毫无关系，判据只有队伍。

    位置一律走 `_seat_body()`（真人取心跳轨迹、bot 取 `battle_pos`），
    这也是判命中时 `_battle_bodies()` 用的同一口径 —— **「挑得中的目标」和
    「打得中的身体」必须是同一批人**，否则 bot 会瞄一个判命中时不存在的人。

    ★ **闯关房（`TEAM_LAYOUT_COOP`）一个都不返回**：那儿大家是队友，
    该打的是怪。怪的句柄服务端手里没有（它们由「控制者」那台机器模拟），
    要打得等 M5 把控制格那条路接起来。

    ★ 个人战（`TEAM_LAYOUT_FREE`）里每个人的队伍都是 `TEAM_NONE`，
    所以判据不能写成「队伍不同」——那样谁都不是敌人。分两种口径来。
    """
    layout = room.team_layout()
    if layout == lobby_module.TEAM_LAYOUT_COOP:
        return []
    seat = room.seats[seat_index] if 0 <= seat_index < len(room.seats) else None
    my_team = None if seat is None else seat.team
    out = []
    for index, other in enumerate(room.seats):
        if other is None or index == seat_index:
            continue
        if layout == TEAM_LAYOUT_TEAMS and other.team == my_team:
            continue
        if _lying_dead(room, index):
            continue
        body = _seat_body(room, index)
        if body is None:
            continue
        # ★★ 烟雾里的人**挑不中**（D67）：别人放了烟，bot 还能隔着云精确
        #    打到里面的人不合理。⚠ 这一条是我们定的，不是原版行为
        #    —— 原版的烟就是一团纯视觉的云，挡的是真人的眼睛。
        if _in_smoke(room, body[0], body[1], _now()):
            continue
        out.append((index, body[0], body[1], body[2]))
    return out


# ---------------------------------------------------------------------------
# ★★★ 自己走位（M5）—— 不再只回放真人的轨迹
# ---------------------------------------------------------------------------
#: 一帧最多推几个 tick 的运动。
#:
#: ★ 它**不是**时序阈值（铁律 10），是一道 fail-safe：bot 的帧由真人的
#: 心跳驱动（§32），两帧之间正常只隔一发心跳 = 4 个 tick（§71）。
#: 真隔了很久（读图、中继断开、房里一时没人发心跳），那段时间 bot 在别人
#: 屏幕上**根本没在动** —— 这时候按流逝时间一次推几百个 tick，表现就是
#: 「唰」地闪到半张图外。宁可少走几步，也不要瞬移。
#:
#: ★★ 会话 41 从 16 收到 **8**（= 两发心跳的量）。用户 2026-08-30 报的
#: 「bot 位置闪来闪去，有时候闪现进墙里 / 闪现穿墙」的后半段就是它：
#: 服务端被 A\* 卡住几百毫秒之后，这里一次补 16 个 tick ≈ **128 个单位**，
#: 而收方是按**走路动画**把远端角色挪过去的（§39）—— 一发心跳里挪不了
#: 那么远，只能瞬移过去，路上有墙也照穿。
#: 8 个 tick 是「两发心跳的位移」，收方还能用走路把它消化掉；
#: 真正的病根（服务端卡住）在 `botnav` 的边缓存那一侧治。
BOT_MOVE_MAX_TICKS = botmove.TICKS_PER_BEAT * 2

#: ★★★ bot **每秒重新做几次决策**（用户 2026-08-30）。
#:
#: 「不需要每一帧都重新计算决策，因为真人也不可能脑内计算那么快，
#:  每秒计算 10 到 20 次就够了，这个频率已经大幅超过人类的反应速度了。」
#:
#: ★ 为什么这不违反铁律 10：它不是「跳过头 N 次」那种**判据**，而是
#:   一个**采样率** —— 和 D17 里 `BOT_FRAME_INTERVAL_S` 同一条豁免：
#:   物理上没有「该重新想一想了」这个事件可等，人的反应速度本身就是
#:   一个频率。判据（往哪走、跳不跳）一个都没变，只是问得没那么密。
#:
#: 代价与收益：一个动作最多晚 `BOT_DECISION_TICKS - 1` 个 tick（32 ms，
#: 走位上约 8 个单位）才做出来；换来的是每帧的 AI 计算量**对折**
#: —— 而那份计算是压在真人转发路径上的（§137 的另一半）。
BOT_DECISIONS_PER_SECOND = 15.0

#: 隔几个 tick 重新决策一次。由上面那个频率算出来，不单独拍一个数。
BOT_DECISION_TICKS = max(1, int(round(botmove.TICKS_PER_SECOND
                                      / BOT_DECISIONS_PER_SECOND)))


def _character_of(machine):
    return chrprops.get(machine.character_id)


# ---------------------------------------------------------------------------
# ★★★ 出生点（§91）—— 和真人**同一套**分配规则
# ---------------------------------------------------------------------------
#: `.map` 里那两类出生点对象的 type（`tools/mapdata.py` 原样抄进产物的
#: `points`）：**101 = 1 队、102 = 2 队**。
SPAWN_TYPE_TEAM_A = 101
SPAWN_TYPE_TEAM_B = 102


def _spawn_points(terrain, team):
    """`team` 能用的出生点表 —— 逐条照抄 `0x473ba8`。

        dec ecx ; je  -> 队伍 1：只要 type 101
        dec ecx ; je  -> 队伍 2：只要 type 102
        否则          -> **101 接着 102 拼成一张表**（个人战 / 闯关都走这条）

    返回 `[(x, y), …]`，顺序就是产物里的顺序（= `.map` 里的对象顺序）。
    """
    points = {} if terrain is None else (terrain.points or {})
    first = list(points.get(SPAWN_TYPE_TEAM_A, ()))
    second = list(points.get(SPAWN_TYPE_TEAM_B, ()))
    if team == 1:
        return first
    if team == 2:
        return second
    return first + second


def _spawn_team(room, seat_index):
    """挑出生点时算的「我是几队」—— `0x405e4b` 那道门。

    原版**只有组队模式才读座位的队伍号**（`0x409df1(描述符) == 1`，
    也就是 `arguments[0] == 1`）；个人战和闯关一律按 **0** 算，
    于是拿到的是 101+102 拼起来的那张全表。
    """
    if room.team_layout() != TEAM_LAYOUT_TEAMS:
        return 0
    seat = room.seats[seat_index] if 0 <= seat_index < len(room.seats) else None
    try:
        return int(getattr(seat, "team", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _spawn_index(room, seat_index):
    """我在这张表里排第几个（0 基）—— 照抄 `0x405e6f` 那个循环：

        for (i = 0; i < 我的座位号; i++)
            if (座位 i 没人)                       continue
            if (组队模式 && 座位 i 的队伍 != 我的)  continue
            rank++

    ⇒ **「同队里座位号比我小、且有人的座位数」**。所以 1 队的第一个人
    永远落在 101 表的第 0 个点上，和真人一模一样。
    """
    team_mode = room.team_layout() == TEAM_LAYOUT_TEAMS
    my_team = _spawn_team(room, seat_index)
    rank = 0
    for index in range(max(0, min(seat_index, len(room.seats)))):
        other = room.seats[index]
        if other is None:
            continue
        if team_mode and int(getattr(other, "team", 0) or 0) != my_team:
            continue
        rank += 1
    return rank


def _spawn_point(room, seat_index, terrain):
    """这个座位**进图时**该站在哪（`0x405e1c` -> `0x473cb2`）；没有表就 `None`。

    `点 = 表[我的名次 % 表长]` —— 取模是原版自己做的（`0x473cde` 那个
    `idiv`），人比出生点多的时候就有人叠在一起，这也是原版行为。
    """
    points = _spawn_points(terrain, _spawn_team(room, seat_index))
    if not points:
        return None
    point = points[_spawn_index(room, seat_index) % len(points)]
    return (float(point[0]), float(point[1]))


def respawn_point(room, seat_index, terrain=None, roll=None):
    """这个座位**重生**落在哪；算不出来返回 `None`。

    ★★ 和进图那次**不是同一条规则**：`0x4fe832` 调的是 `0x473d2f`，
    而它传下去的队伍号是 **0** —— 也就是 **101+102 拼起来的全表里随机抽
    一个**（`0x473d5f` 那发 `rand() % 表长`），和自己是几队无关。

    语料实证（§91）：1360 发真人 `0x0413` 里，重生坐标的 x **中位差 0**
    （和某个出生点的 x 完全相等），y 比对象低 55 左右（掉到地面上）；
    而「同一条连接 + 同一张图 + 同一座位」的 126 组里有 **68 组两类点都
    落过** —— 队伍过滤在重生这条路上确实不存在。

    `roll` 是给单测钉死随机数用的（`roll(n) -> 0..n-1`），默认 `random`。
    """
    if terrain is None:
        terrain = _terrain(room)
    points = _spawn_points(terrain, 0)
    if not points:
        return None
    picker = random.randrange if roll is None else roll
    point = points[picker(len(points))]
    return (float(point[0]), float(point[1]))


def _settle_spawn(terrain, machine, point):
    """把出生点**放到地面上**：对象在编辑器里挂在半空，角色是掉下去站住的。

    语料实证（§91）：真人报上来的重生 y 比对象的 y 低 **55**（中位），
    p10/p90 是 −44 / 135 —— 正是「从对象那一点往下掉到最近的地面」。
    """
    body = botmove.Body(float(point[0]), float(point[1]), on_ground=False)
    return botmove.settle(terrain, body, _character_of(machine))


def _spawn_overlaps(room, seat_index, point, character):
    """这个复活候选点会不会和另一个活角色叠在一起。

    判据就是 `ChrProps.ini` 的三个碰撞圆；不额外拍“至少隔多少”。
    同一格里几个 bot 同时到期时，前一个的 `pending_spawn` 已经写好
    但身体还没在下一格搬过去，所以这里优先读它，免得两个人
    挑中同一个空位。
    """
    mine = character.circles(float(point[0]), float(point[1]), False)
    for index, seat in enumerate(room.seats):
        if seat is None or index == seat_index or _lying_dead(room, index):
            continue
        conn = seat.conn
        pending = getattr(conn, "pending_spawn", None)
        if pending is not None:
            other = (float(pending[0]), float(pending[1]), False)
        else:
            other = _seat_body(room, index)
        if other is None:
            continue
        theirs = chrprops.get(seat.character_id).circles(
            other[0], other[1], bool(other[2]))
        for ax, ay, ar, _aname in mine:
            for bx, by, br, _bname in theirs:
                if math.hypot(ax - bx, ay - by) < ar + br:
                    return True
    return False


def _columns_by_distance(center, width):
    """按到 `center` 的**水平距离从近到远**产出列号 `0..width-1`。

    ★ 这个顺序是 `_nearest_front_spawn()` 能提前收工的**前提**：
      产出是按 `|x − center|` 单调不减的，所以「这一列本身就比已有的
      最优候选还远」的那一刻，后面每一列只会更远 —— 整个扫描到此为止。
      两边各一个游标做归并，`center` 是浮点也照样精确排序。
    """
    left = int(math.floor(center))
    right = left + 1
    if left >= width:
        left, right = width - 1, width
    if right < 0:
        left, right = -1, 0
    while left >= 0 or right < width:
        if left < 0:
            yield right
            right += 1
        elif right >= width:
            yield left
            left -= 1
        elif center - left <= right - center:
            yield left
            left -= 1
        else:
            yield right
            right += 1


def _nearest_front_spawn(room, seat_index, terrain, machine, anchor):
    """离带头真人最近的**有效可站人处**；找不到返回 `None`。

    搜索用的是当前动态地形的全部站立面，所以尚未打碎的罐子、
    已打开的空档和刚长回来的物件都自动是对的。排序口径是：
    欧氏距离 -> 竖直差 -> 水平差 -> 坐标，因此同一份地图上结果稳定。

    ## ★★★ 从带头真人那一列**向两边发散**扫，不是从 x=0 扫到头

    这个函数跑在**游戏主线程**上（重生看门狗），而任务图很宽 ——
    `Quest03_1` 是 11400 列 / 15029 个站立面。从 x=0 往右扫的话，
    在越过锚点之前 `best` 一路都在改善，剪枝一格都剪不掉：实测
    带头真人在图中部时**一次调用 99.5 ms**、`fits()` 被调 5982 次，
    等于一次复活吞掉三格房间循环（§137 把 A\\* 挪去后台就是治这个病）。
    发散着扫、一超过当前最优就 `break`，答案一模一样，代价只和
    「锚点附近有多空」有关。
    """
    if terrain is None or anchor is None or terrain.width <= 0:
        return None
    who = _character_of(machine)
    ax, ay = float(anchor[0]), float(anchor[1])
    best = None
    for x in _columns_by_distance(ax, terrain.width):
        dx = float(x) - ax
        # 水平差单独就超过已有最优的总距离 ⇒ 这一列和它**之后的每一列**
        # 都不可能更好（产出是按 |x − ax| 排的）。
        if best is not None and dx * dx > best[0][0]:
            break
        for y in terrain.surfaces(x):
            point = (float(x), float(y))
            dy = point[1] - ay
            rank = (dx * dx + dy * dy, abs(dy), abs(dx), point[0], point[1])
            if best is not None and rank >= best[0]:
                continue
            if not botmove.fits(terrain, point[0], point[1], who):
                continue
            if _spawn_overlaps(room, seat_index, point, who):
                continue
            best = (rank, point)
    return None if best is None else best[1]


def pick_respawn_point(room, seat_index):
    """★ 给 `gameserver` 的重生看门狗用：这个 bot 该在哪站起来（§91）。

    挂在 `gameserver.BOT_RESPAWN_POINT` 上。做两件事，**必须一起做**：

    1. 返回坐标 —— 看门狗把它填进 `0x0419`，客户端照着把模型放过去；
    2. 把同一个坐标记进 `BotConn.pending_spawn` —— 下一帧 `_own_step()`
       把身体挪过去。少做第 2 件的话，客户端把 bot 放在出生点，
       而心跳还在报死亡地点，模型当场被拽回去。

    算不出来（这张图没有出生点 / 座位上不是 bot）就返回 `None`，
    看门狗退回它原来那套兜底。
    """
    seat = room.seats[seat_index] if 0 <= seat_index < len(room.seats) else None
    machine = None if seat is None else seat.conn
    if not isinstance(machine, BotConn):
        return None
    terrain = _terrain(room)
    point = None
    point_settled = False
    # ★★★ 任务模式不再从全图随机表里抽：用户要求 bot 在
    #   **推进最靠前的真人**附近站起来。“最靠前”复用任务牵引绳
    #   的 `_quest_forward + _coop_leader`，两条链不会对“前”有两种说法。
    if room.team_layout() == lobby_module.TEAM_LAYOUT_COOP and terrain is not None:
        leader = _coop_leader(room, _quest_forward(terrain))
        anchor = None if leader is None else tuple(leader.sync_trail[-1][:2])
        point = _nearest_front_spawn(room, seat_index, terrain, machine, anchor)
        point_settled = point is not None
        if point is None and anchor is not None:
            # 极端地图没找到不重叠的空位：带头真人的脚下至少是
            # 一个真客户端已经证明可站的点。允许短暂叠人，也不退回后方。
            body = _settle_spawn(terrain, machine, anchor)
            if (body is not None and body.on_ground
                    and botmove.fits(terrain, body.x, body.y, _character_of(machine))):
                point = (body.x, body.y)
                point_settled = True
        if point is not None:
            machine.log(f"   任务重生点: 带头真人 @ "
                        f"({anchor[0]:.0f}, {anchor[1]:.0f}) -> "
                        f"({point[0]:.0f}, {point[1]:.0f}) 最近有效站立面")
    # PVP 照原版的全表随机；任务模式拿不到真人位置/地形时也
    # 退回这条已验证的老路，不让复活整个失效。
    if point is None:
        point = respawn_point(room, seat_index, terrain, roll=machine.roll)
    if point is None:
        return None
    if terrain is not None and not point_settled:
        body = _settle_spawn(terrain, machine, point)
        point = (body.x, body.y)
    machine.pending_spawn = point
    return point


# ---------------------------------------------------------------------------
# ★★★ 视野（V0.3 §127）—— 屏幕外的人 bot「看不见」，只知道大概方位
# ---------------------------------------------------------------------------
#: bot 看得见的范围：以自己为中心的**半宽 / 半高**（世界单位）。
#:
#: 用户 2026-08-30：「离得很远时 bot 都能朝我的方向准确开枪，这不合理。
#: 真人只能看见自己屏幕内的人，远在屏幕外就只能盲狙。……这个范围可以比
#: 真正的屏幕稍大一点，因为真人可以把准星移到屏幕边缘，镜头会跟着挪一点。」
#:
#: ★★ 这两个数**不是照分辨率拍的，是从语料量出来的** —— 而且量的正好就是
#: 「准星能落到多远」这件事，镜头跟着准星挪的那一截天然含在里面：
#:
#:     144 万发心跳（380 份语料）里 `|准星 − 角色|` 的分位数
#:       x 方向  p50=426  p75=672  p90=864  **p95=962**
#:       y 方向  p50=143  p75=253  p90=403  **p95=522**
#:
#: 准星永远在屏幕里，所以「准星最远能离我多远」= 「我最远能看多远」。
#: 交叉验证：§48 量的**打中人的距离** p99 = 1015、最大 1163 —— 两把完全
#: 独立的尺子都落在 1000 上下，说明这就是真人的可视半径。
#: 用户 2026-09-01 明确改成「以 bot 为中心，上下左右各一个
#: 1024x768 屏幕」，所以整个框是 **2048 x 1536**。这两个是
#: 半宽/半高，不要再除以 2。
BOT_VISION_HALF_X = 1024.0
BOT_VISION_HALF_Y = 768.0

#: 视野内没有敌人时的血量滞回（用户 2026-09-01 拍板）。
#: 严格小于 25% 才转隐蔽，严格大于 50% 才转逼近；中间保持
#: 上一姿态，治掉治疗/伤害在边界上让 bot 来回掉头。
BOT_BLIND_HIDE_BELOW = 0.25
BOT_BLIND_PRESS_ABOVE = 0.50


def _in_sight(machine, x, y):
    """`(x, y)` 在 bot 的视野框里吗。位置未知时一律**看不见**。"""
    body = machine.battle_pos
    if body is None:
        return False
    return (abs(float(x) - body[0]) <= BOT_VISION_HALF_X
            and abs(float(y) - body[1]) <= BOT_VISION_HALF_Y)


def _visible_targets(room, machine, seat_index):
    """`_hostile_targets()` 里**看得见**的那一批（§127）。"""
    return [row for row in _hostile_targets(room, seat_index)
            if _in_sight(machine, row[1], row[2])]


def _rough_bearing(room, machine, seat_index):
    """屏幕外的敌人只剩**上下左右**这么粗的方位；一个敌人都没有返回 `None`。

    用户 2026-08-30：「为防止所有人都在范围外导致 bot 找不到人而不知道
    干什么，可以告诉 bot 敌人的粗略方位，上下左右这个粗略程度就够了。」

    ⇒ 返回的是一个**视野边缘上的点**，不是敌人的真实坐标：bot 朝那儿挪，
    挪到人进了视野框，`_enemy_spot()` 才重新给出精确位置。
    """
    rough = _rough_bearing_raw(room, machine, seat_index)
    return None if rough is None else rough[0]


def _rough_bearing_raw(room, machine, seat_index):
    """`_rough_bearing()` 的完整答案：`(粗方位点, 左右符号)`。

    第二格是「最近那个敌人在我**左边还是右边**」（`-1/0/1`）。粗方位点
    在竖直占优时 x 和自己完全相同，`_walk_to()` 那句
    `direction = 0 if abs(delta_x) < 1.0` 于是恒为 0 —— A\\* 再解不出
    上下层的路（一张图的可达分量往往只覆盖一半，§137），bot 就**杵在
    原地**，正是用户 2026-09-01 要治的那个症状。这一格让
    `_blind_intent()` 在那种时候还能横着走去找上去的路，而**不泄露
    任何距离**：只有一个符号。
    """
    body = machine.battle_pos
    if body is None:
        return None
    best = None
    candidates = [(tx, ty) for _index, tx, ty, _crouched
                  in _hostile_targets(room, seat_index)]
    if room.team_layout() == lobby_module.TEAM_LAYOUT_COOP:
        # 任务模式的“敌人”是怪。`live_mobs` 的位置和真人客户端
        # 拿到的是同一发 rpAiMsg；这里也只把它压成一个方向。
        candidates.extend((tx, ty) for tx, ty, _handle in live_mobs(room))
        quest = None if room is None else room.quest
        gun = getattr(quest, "boss_gun", None)
        if gun is not None:
            candidates.append((float(gun[0]), float(gun[1])))
    for tx, ty in candidates:
        span = math.hypot(tx - body[0], ty - body[1])
        if best is None or span < best[0]:
            best = (span, tx, ty)
    if best is None:
        return None
    _span, tx, ty = best
    dx, dy = tx - body[0], ty - body[1]
    side = 0 if abs(dx) < 1.0 else int(math.copysign(1, dx))
    # ★ 只给**一个**主方向，不给对角线。用各自的视野半径归一化
    #   之后比，否则 1024x768 的非正方形视野会偏爱 x 方向。
    nx = abs(dx) / BOT_VISION_HALF_X
    ny = abs(dy) / BOT_VISION_HALF_Y
    if nx >= ny and abs(dx) >= 1.0:
        return ((body[0] + math.copysign(BOT_VISION_HALF_X, dx), body[1]),
                side)
    if abs(dy) >= 1.0:
        return ((body[0], body[1] + math.copysign(BOT_VISION_HALF_Y, dy)),
                side)
    return None


def _enemy_spot(room, machine, seat_index):
    """离自己最近的、**看得见的**那个敌人：`(座位号, (x, y))`；没有返回 `None`。

    ★ 闯关房恒为 `None`（`_hostile_targets` 那边就是空的）—— 那儿该打的是
    怪，走位归 `_coop_intent()`。
    """
    if machine.body is None:
        return None
    best = None
    for index, tx, ty, _crouched in _visible_targets(room, machine, seat_index):
        span = abs(tx - machine.body.x)
        if best is None or span < best[0]:
            best = (span, index, (float(tx), float(ty)))
    return None if best is None else (best[1], best[2])


# ---------------------------------------------------------------------------
# ★★★ 逼近还是拉开（M5-C）—— 判据是「照这样打下去谁先倒」
# ---------------------------------------------------------------------------
#: 姿态翻转的两道**迟滞**门（不是时间阈值，是决策边际，铁律 10 不管这个）。
#:
#: 正在逼近时，要「我明显更吃亏」才后撤；正在后撤时，要「我明显更占便宜」
#: 才回头。两个数不对称就不会在临界点上来回抖。
BOT_RETREAT_RATIO = 1.25
BOT_PRESS_RATIO = 0.80

#: 一次后撤最多往回退多远（世界单位）。
#:
#: 出处是 §48 那份真人交战距离分布：命中距离 p10 = 264、中位 616。
#: 退掉半个中位数 ≈ 一次有效的脱离，而不是「跑到图那头去」——
#: 真人拉开距离也就是退一个屏幕宽的一半。
BOT_RETREAT_SPAN = 320.0

#: 找后撤落脚点时的扫描粒度 = A\* 的路点分辨率，省得挑出一个规划器分不开的点。
BOT_RETREAT_STEP = botnav.KEY_X


def _seat_weapon(room, seat_index):
    """这个座位此刻拿的是哪把枪；不知道就退回他那个角色的缺省枪。

    * bot —— 直接问它自己（`BotConn.weapon`）；
    * 真人 —— `rpChangeWeapon` / `rpFire` 里带着 ammo id，`note_peer_hit()`
      顺手记在 `conn.peer_weapon` 上。一发都还没见过时退回
      `weapondata.preferred_for(角色)` —— 那正是客户端建角色时给的那把。
    """
    seat = room.seats[seat_index] if 0 <= seat_index < len(room.seats) else None
    conn = None if seat is None else seat.conn
    if conn is None:
        return None
    if getattr(seat, "is_bot", False):
        return getattr(conn, "weapon", None)
    weapon = getattr(conn, "peer_weapon", None)
    if weapon is not None:
        return weapon
    return weapondata.preferred_for(seat.character_id)


def _seconds_to_kill(room, shooter, victim, span, victim_speed=None):
    """`shooter` 把 `victim` 打倒要几秒；打不到返回 `None`。

    两边用**同一把尺**：`botarms.score()` 算出「每秒有效伤害」，
    再拿血量台账里 victim 还剩的血一除。所以「我血多但枪软」和
    「我血少但枪硬」这两种局面能放在一起比 —— 这正是用户要的
    「根据自身血量和敌人血量判断」。
    """
    weapon = _seat_weapon(room, shooter)
    if weapon is None:
        return None
    body = _seat_body(room, victim)
    shooter_body = _seat_body(room, shooter)
    if body is None or shooter_body is None:
        return None
    shot = ballistics.solve(
        weapon, span, 0.0, speed=_lob_speed(weapon, span, 0.0))
    if shot is None:
        return None
    speed = (nominal_speed(room, victim) if victim_speed is None
             else float(victim_speed))
    radius = _hit_radius(room, victim, weapon)
    dps = botarms.score(weapon, shot, speed, radius, span,
                        damage_scale=_damage_scale(room))
    if not dps or dps <= 0.0:
        return None
    ledger = _health(room)
    left = (_seat_max_hp(room, victim) if ledger is None
            else ledger.remaining(victim, _seat_max_hp(room, victim)))
    return max(0.0, left) / dps


def nominal_speed(room, seat_index):
    """这个座位「正常交火时」的走速（单位 / tick）—— `ChrSpeed`。"""
    seat = room.seats[seat_index] if 0 <= seat_index < len(room.seats) else None
    return botmove.walk_speed(
        chrprops.get(0 if seat is None else seat.character_id))


def _stance(room, machine, seat_index, enemy_index, span):
    """这一刻该逼近还是该拉开 —— 返回 `"press"` / `"retreat"`。

    ## 判据

    `我打倒他要几秒` 和 `他打倒我要几秒` 一比：我更快就压上去，他更快就
    拉开。两边的「几秒」都是「还剩多少血 ÷ 每秒有效伤害」，血量来自
    `bothp` 那本账（每台客户端本来就在记的同一份，见那个模块的说明），
    输出来自 `botarms`（原版的伤害 / 射速 / 溅射 + 几何命中概率）。

    ## 为什么这一条不违反铁律 11

    「保持交战距离」那条（D47）是**我们凭空发明**的，用户当场否掉了。
    这一条不一样：用户 2026-08-29 明确要求「bot 要根据自身血量和敌人血量
    自己判断应该靠近敌人还是应该拉远距离」。⇒ 出处是**用户的需求**，
    而具体怎么判由数据算，不拍脑袋定「血少于三成就跑」这种常数。

    算不出来（对面还没露过面、这把枪够不着）时**保持原来的姿态** ——
    没有新事实就不做新决定。
    """
    # ★★★ 两边喂的速度**故意不对称**，这不是笔误：
    #   * 「我要几秒打倒他」—— 他动多快是**外生**的事实，用实测速度；
    #   * 「他要几秒打倒我」—— 我动多快是**这个决定本身的结果**，
    #     拿实测速度会形成自反馈：「我在退 ⇒ 更难打中 ⇒ 其实我占优 ⇒
    #     压上去 ⇒ 停下 ⇒ 又好打了」，实测每 4 个 tick 翻一次（会话 40）。
    #     ⇒ 用名义走速，问的是「正常交火下谁先倒」。
    mine = _seconds_to_kill(room, seat_index, enemy_index, span,
                            victim_speed=math.hypot(
                                *_seat_velocity(room, enemy_index)))
    theirs = _seconds_to_kill(room, enemy_index, seat_index, span,
                              victim_speed=nominal_speed(room, seat_index))
    if mine is None or theirs is None:
        return machine.stance
    if machine.stance == "retreat":
        if mine <= theirs * BOT_PRESS_RATIO:
            machine.stance = "press"
            machine.retreat_goal = None
    elif mine > theirs * BOT_RETREAT_RATIO:
        machine.stance = "retreat"
        machine.retreat_goal = None
    return machine.stance


def _retreat_spot(room, machine, terrain, enemy):
    """挑一个「离他更远、最好还有掩体」的落脚点；挑不出来返回 `None`。

    从自己脚下**朝背离敌人的方向**一格一格往外扫，每一格问这一列有没有
    够得着的站立面。第一个**把弹道挡住**的点直接选中（那就是掩体，
    躲在它后面正是真人拉开距离时干的事）；一个掩体都没有就选扫到的最远点。

    ⚠ 判「挡住没有」用的是 `line_blocked`（`blocks_bullet`，单向平台不挡
    子弹，§29）—— 拿 `is_solid` 会把一根白线当掩体。

    ## ★★★★★ 已经躲在掩体后面了 —— **答案就是脚下这一点**（V0.3 §165）

    `_blind_cover_spot()` 开头本来就有这一句（「已经在掩体后面，当前点就是
    最近答案」），这儿一直漏着，后果是**每一格都挑出一个只有一个扫描格
    （8 像素）远的「掩体」**：站着的人本来就被挡住了，往后挪 8 像素当然
    还是被挡住，于是 `index=1` 那一格立刻 `return`。

    接着 `_retreat_done()` 一看「离目标只有 8 像素 ≤ `GOAL_X`(64)」判「退到了」
    ⇒ 下一格重挑 ⇒ 又是 8 像素。而 bot 一格决策走 14~21 像素，**一步就迈过头**。
    贴到图边时更糟：`cx < 0` 直接 `break`，挑不出任何点 ⇒ 走位改成朝敌人压上去
    ⇒ 走回来又够得着一格了 ⇒ 再退。**三格一个循环 = 12 tick = 3 发心跳**，
    正是实机量到的周期（`Esperan03` 01:11:14.989~17.815 座位 1
    x = 3/7/10 来回、01:10:25.9~32.7 座位 5 同样；V0.3 §164 ③ 在图两边
    都见过）。用户 2026-09-02：「好几个 bot 走路都感觉一卡一卡的。」
    """
    body = machine.body
    if body is None or terrain is None:
        return None
    mx, my = _muzzle(body.x, body.y, enemy[0])
    if terrain.line_blocked(mx, my, enemy[0], enemy[1] - BOT_MUZZLE_HEIGHT):
        return (body.x, body.y)
    away = 1.0 if body.x >= enemy[0] else -1.0
    reach = botmove.walk_speed(_character_of(machine)) * botmove.CLIMB_SLOPE         * botmove.TICKS_PER_BEAT
    farthest = None
    steps = max(1, int(BOT_RETREAT_SPAN / BOT_RETREAT_STEP))
    for index in range(1, steps + 1):
        cx = body.x + away * BOT_RETREAT_STEP * index
        if cx < 0 or cx >= terrain.width:
            break
        sy = botmove.surface_near(terrain, cx, body.y, reach)
        if sy is None:
            continue
        farthest = (cx, float(sy))
        mx, my = _muzzle(cx, sy, enemy[0])
        if terrain.line_blocked(mx, my, enemy[0], enemy[1] - BOT_MUZZLE_HEIGHT):
            return farthest                # 有掩体，就近躲进去
    return farthest


def _clear_navigation(machine, failed=None):
    """清掉逐帧路线；`failed` 可记一份“同一事实下别立刻重算”的签名。

    ## ★★★ 腾空时**不动**二段跳那面旗（V0.3 §151）

    「这一段飞行还欠第二跳」说的是**正在进行的这一段抛物线**，不是路线。
    而 `_move_intent()` 里有一串早退分支（躲子弹 / 打得到就站住 / 后撤 /
    闯关那几条）都会调到这里，它们**都不看在不在空中** —— bot 飞在岩浆
    上方时只要命中任何一条，第二段就永远补不上，一段跳掉进去。
    ⇒ 判据是**在不在地上**这个事实：脚一沾地 `_walk_to()` 那句就把它清了，
      根本轮不到这里。
    """
    machine.nav_path = []
    machine.nav_goal = None
    machine.nav_started = False
    machine.nav_failed = failed
    body = machine.body
    if body is None or body.on_ground:
        machine.nav_double_jump = False
    # ★ 在算的那张单子也作废：目标已经不作数了，算回来也用不上（§137）。
    botplan.forget(machine)


def _nav_signature(terrain, body, spot):
    """一次规划请求的空间事实签名；没有挂钟/帧计数。

    ★★★★ **破坏物的存活集合也算一项事实**（V0.3 §172）。以前只带图名，
    而一张图的所有变体名字是一样的 ⇒ 罐子碎了 / 长回来了，「这一组事实下
    别重算」的闩**一点都不松**。而这个闩的解法本来就写着「挡路物自己翻转
    就把全房间放开」—— 那句话此前只对 `path_breakable` 成立，对
    `nav_failed` 不成立。少这一项的代价：bot 站在那儿等着地形变，
    地形真变了它也不知道。
    """
    return (getattr(terrain, "name", None),
            getattr(terrain, "alive", None),
            int(round(body.x / botnav.KEY_X)),
            int(round(body.y / botnav.KEY_Y)),
            int(round(spot[0] / botnav.KEY_X)),
            int(round(spot[1] / botnav.KEY_Y)))


def _route_intent(machine, terrain, who, spot, hold_at_breakable=False):
    """规划/执行一条可达路线；找不到返回 `None`。

    ## ★★★ `hold_at_breakable` —— 「站住打挡路物」只属于破障那个调用者

    锁着一件挡路物（`path_breakable`）而安全前缀又走空时，**只有**
    `_breakable_move_intent()` 那条路该回「原地不动」—— 它前面有
    `_breaking_now()` 那道门，确认了这一帧真的是冲着挡路物去的。

    ⚠ 这一句以前对**所有**调用者生效，而且排在规划分支前面，于是
    `_walk_to()` 那条正常走位一旦碰上这个状态就永远回「不动」，
    **连规划单都不再递**（V0.3 §160）。解锁只剩两条路：挡路物自己
    翻转（`_refresh_breakables` 把全房间的锁一起清），或者移动目标挪出
    `GOAL_X`。而对战房里 bot 的移动目标就是别的 bot —— 它们同样被钉住，
    目标不动 ⇒ 谁也解不开 ⇒ **整个房间一起僵住，直到某个罐子碎 / 长回来
    把所有人一起放开**。用户 2026-09-01 22:53 实机看到的「所有 bot 都不动，
    一分钟后又都恢复」「卡几秒后突然一起动」就是它。

    ## ★★ 一帧最多规划一次

    `_own_step()` 逐 tick 问意图（§120）—— 那条是为了不把「起跳 / 按 ↓」
    这种**一次性动作**整批丢掉。但「走哪条路线」是一整帧都成立的决定，
    逐 tick 重算纯属白算：一次 `botnav.plan()` 在冷缓存下要几百毫秒，
    一帧问 16 次就是几秒。实机日志里同步转发 `max=4756 ms`（真人的位置包
    被堵在后面 = 屏幕卡顿）和 bot 的「闪现」都是它。
    ⇒ 缓存键用**帧序号**（和 `_dodge_intent` 同一个套路，也是同一个理由：
      Win7 的 3.8 上 `time.monotonic()` 粒度 15.6 ms，挂钟当不了键）。
    """
    body = machine.body
    if body is None or not body.on_ground:
        # 正在被击飞而不是执行路径边：等事实重新落地后再规划。
        if not machine.nav_started:
            _clear_navigation(machine)
        elif machine.nav_path:
            step = machine.nav_path[0]
            # ★★ 带二段跳的边（普通跳 / 弹跳台都可能带）：飞到**顶点**
            #    （`v.y >= 0` 的第一个 tick）再按一次。判据和规划这条边时
            #    用的是同一句 —— 两边不一致的话落点就对不上。
            again = machine.nav_double_jump and botnav.at_apex(body)
            return (step.direction, again, False, step.fast_run)
        return None

    if machine.nav_goal is not None:
        shifted = math.hypot(spot[0] - machine.nav_goal[0],
                             spot[1] - machine.nav_goal[1])
        if shifted > botnav.GOAL_X:
            _clear_navigation(machine)
            machine.path_breakable = None
            machine.path_breakable_prefix = []
            machine.path_breakable_only = False

    # 吃掉已经走到的边；动作完成是“重新落在规划落脚点”这个事实，不看时间。
    while machine.nav_path and botnav.step_reached(body, machine.nav_path[0]):
        machine.nav_path.pop(0)
        machine.nav_started = False
        machine.nav_double_jump = False
    if (hold_at_breakable and not machine.nav_path
            and machine.path_breakable is not None):
        # 捷径的安全前缀已经走完：这里就是对挡路物开火的位置。
        # ★ 只对破障那个调用者成立，见函数抬头。
        return (0, False, False, False)
    if not machine.nav_path:
        signature = _nav_signature(terrain, body, spot)
        if machine.nav_failed == signature:
            return None
        # ★★★ A\* **在后台线程上跑**（§137）：这里只做两件 O(1) 的事 ——
        #     看看上一张单子算好了没有、没有就递一张新的。
        choice = botplan.take_result(machine, body, spot)
        if choice is None:
            if machine.nav_planned_at != machine.frame_seq:
                # ★ 一帧最多递一张单（`_own_step` 是**逐 tick**问意图的，
                #   §120）。判据是「这一帧」这个事件，不是挂钟。
                machine.nav_planned_at = machine.frame_seq
                opened = (terrain.variant(())
                          if getattr(terrain, "breakables", ())
                          and getattr(terrain, "alive", frozenset())
                          else None)
                botplan.ask(machine, terrain, body, who, spot,
                            open_terrain=opened)
            return None
        if choice.blocker is not None:
            machine.path_breakable = int(choice.blocker)
            machine.path_breakable_prefix = list(choice.prefix)
            machine.path_breakable_only = bool(choice.stranded)
            machine.nav_path = list(choice.prefix)
        else:
            machine.path_breakable = None
            machine.path_breakable_prefix = []
            machine.path_breakable_only = False
            machine.nav_path = list(choice.path)
        if not machine.nav_path and not choice.reached and choice.blocker is None:
            _clear_navigation(machine, failed=signature)
            return None
        machine.nav_goal = (float(spot[0]), float(spot[1]))
        machine.nav_started = False
        machine.nav_double_jump = False
        machine.nav_failed = None
        if not machine.nav_path:
            if hold_at_breakable or machine.path_breakable is None:
                # 已经站在目标上（或者破障那条正等着开火）—— 站住是对的。
                return (0, False, False, False)
            # ★ 后台给的是「先打碎它」那条捷径，而这一帧并不是冲着挡路物
            #   去的（`_breaking_now()` 已经否了 / 手上这把枪够不着）。
            #   **别把人钉在这儿** —— 回 `None`，让 `_walk_to()` 那套老兜底
            #   照常把腿迈出去。同一组空间事实下不重复递单：签名和「A\* 说
            #   到不了」用的是同一个（空间事实，不是挂钟，铁律 10）。
            machine.nav_failed = signature
            return None

    step = machine.nav_path[0]
    if machine.nav_started:
        # 上一帧已经离地、这一帧又落在别处 = 这条物理边没按规划结束，重算。
        if body.on_ground:
            _clear_navigation(machine)
            return _route_intent(machine, terrain, who, spot,
                                 hold_at_breakable=hold_at_breakable)
        return (step.direction, False, False, step.fast_run)
    # ★ 「这条边要不要在顶点补第二段」是 `Step.double` 说了算，和起跳那一下
    #   按什么键分开记 —— 弹跳台那一条**起跳不能按跳**（按了人先离地，
    #   台子就轮不到了），但顶点照样可以再来一段。
    machine.nav_double_jump = bool(step.double)
    if step.action in (botnav.ACTION_JUMP, botnav.ACTION_DOUBLE_JUMP):
        return (step.direction, True, False, step.fast_run)
    if step.action == botnav.ACTION_DROP:
        return (0, False, True, False)
    if step.action == botnav.ACTION_PAD:
        # 人已经站在台上；普通 tick 会让台子自己把它弹出去（§99）。
        return (0, False, False, False)
    return (step.direction, False, False, False)


# ---------------------------------------------------------------------------
# ★★★ 躲子弹（M5-E）—— 场上在飞的敌弹，服务端本来就都看得见
# ---------------------------------------------------------------------------
def _threats_against(room, machine, seat_index):
    """此刻可能打到这个 bot 的**在飞的弹**（`botthreat.Threat` 列表）。

    两个来源都是现成的账，一发新的逆向都不用：

    * **真人打的** —— `note_peer_fire()` 把每一发 `rpFire` 解成 `Shot` 存进
      `conn.peer_shots`（§92 拿它反推击退用的就是这一份），M5-E 只多要了
      一个「什么时候出膛的」；
    * **别的 bot 打的** —— 那本来就是服务端自己算的弹体（D28），
      `BotConn.pending_shots` 里躺着的就是它。

    ★ **按碰撞排除组过滤**（§63）：同组的弹体压根撞不着自己，躲它是白躲。
    个人战里每个人一组 ⇒ 谁的弹都得躲；组队战里队友的弹自动被排除。
    """
    if machine.body is None:
        return []
    mine = _seat_group(room, seat_index)
    out = []
    for index, seat in enumerate(room.seats):
        if seat is None or index == seat_index:
            continue
        friendly = _seat_group(room, index) == mine
        conn = seat.conn
        if conn is None:
            continue
        if getattr(seat, "is_bot", False):
            if friendly:
                continue
            for shell in getattr(conn, "pending_shots", ()) or ():
                out.append(botthreat.Threat(
                    index, shell.weapon, shell.x0, shell.y0, shell.shot,
                    shell.born, ("shell", shell.handle)))
            continue
        for record in getattr(conn, "peer_shots", ()) or ():
            # ★★ 队友的子弹撞不着自己（§63），躲它是白躲 —— **但怪的子弹
            #    是队友那台机器替它发的**（`rpFire body+0 == 20`，§23），
            #    那一发照样打得死人。闯关模式里要躲的正是这一批（M5-G）。
            if friendly and record.source not in MOB_FIRE_SOURCES:
                continue
            out.append(botthreat.Threat(
                index, record.weapon, record.x, record.y, record.shot,
                record.at, ("peer", record.serial)))
    return out


def _dodge_intent(room, machine, seat_index, terrain, now):
    """这一帧要不要为了躲子弹改变动作；不用躲返回 `None`。

    返回值和 `_move_intent()` 同格式 `(方向, 起跳, 下落, 冲刺跑)`，
    外加把「蹲不蹲」写进 `machine.dodge_crouch`（心跳里蹲是单独一发事件包，
    §41，所以不能塞进这个四元组）。

    ## 掷骰子的判据是**这一波威胁**，不是每一帧

    `dodge_error` 掷中时 bot 随便挑一个动作（可能就是站着不动）——
    真人预估错弹道的样子就是这个。判据换成「威胁集合变了没有」这个事实
    （铁律 10 的去重口径）：同一波弹只判断一次，新的一发进场才重掷。
    """
    # ★★ **一帧只判一次**：`now` 是 `_tick_bot()` 这一帧算好传下来的，
    #    所以拿它当缓存键正好等于「这一帧」。躲避是「按住哪几个键」这种
    #    决定，真人也不会 32 ms 换一次；而每判一次要跑十个候选 × 24 个 tick
    #    的物理模拟，逐 tick 判是四倍开销。
    #    ★ 缓存键用**帧序号**而不是 `now`：Win7 运行时（3.8）的
    #      `time.monotonic()` 粒度是 15.6 ms，两帧可能拿到**同一个读数**
    #      —— 那样缓存就永远不失效了（铁律 10：判据要是事件，不是挂钟）。
    #    ⚠ 和 §120 那条「逐 tick 重新决策」不冲突：那条治的是**一次性动作**
    #      （起跳 / 按 ↓）被整批丢掉，这里的动作在整帧里是一直按着的。
    if machine.dodge_at == machine.frame_seq:
        machine.dodge_crouch = machine.dodge_cached_crouch
        return machine.dodge_cached
    machine.dodge_at = machine.frame_seq
    machine.dodge_crouch = False
    machine.dodge_cached = None
    machine.dodge_cached_crouch = False
    if terrain is None or machine.body is None:
        machine.dodge_signature = None
        return None
    threats = _threats_against(room, machine, seat_index)
    signature = tuple(sorted(threat.key for threat in threats))
    if signature != machine.dodge_signature:
        machine.dodge_signature = signature
        machine.dodge_blind = botthreat.roll_blind(
            machine.roll, difficulty_profile(room)["dodge_error"])
    if not threats:
        return None
    option = botthreat.choose(terrain, machine.body, _character_of(machine),
                              threats, now, blind_pick=machine.dodge_blind)
    if option is None:
        return None
    machine.dodge_crouch = option.crouched
    machine.dodge_cached_crouch = option.crouched
    machine.dodge_cached = (option.direction, option.want_jump,
                            option.want_drop, option.fast_run)
    return machine.dodge_cached


def _breakable_pinning_body(machine, terrain):
    """身体**此刻真的被破坏物压住**了吗 —— 压住它的那一件，否则 `None`。

    ## ★★★ 判据是两句 `fits()` 的**地形差**，不是「碰没碰到掩码」

    `Breakable.hit()` 抄的是客户端 `HitTest`，自带 **3×3 邻域** ——
    拿碰撞圆的上下左右边缘点去问它，「站在罐子**旁边**」和「被罐子
    裹住」返回的是同一个答案。实测截图那张 `CamelCulvert04`：
    3514 个可站落脚点里 **571 个（16.2%）**会被这么误判成「被困住」，
    bot 于是站住打一件根本不挡路的罐子。

    真正区分这两种情况的事实是「**这个位置塞不塞得下人**」，而这句话
    `botmove.fits()` 已经在回答了（§152）。所以：这儿塞不下（`fits`
    说的）、而**把破坏物全拿掉就塞得下**（同一句话问 `variant(())`）
    ⇒ 压住我的只可能是破坏物。两句都是几何事实，没有第二套近似。

    ★ 排在 `_unstick_intent()` **之前**用（见 `_move_intent`）：人被
      实心掩码裹着的时候脱困没有方向可走，唯一的出路是把它打碎。
    """
    body = machine.body
    if body is None or terrain is None:
        return None
    alive = getattr(terrain, "alive", frozenset())
    if not alive or not getattr(terrain, "breakables", ()):
        return None
    who = _character_of(machine)
    if botmove.fits(terrain, body.x, body.y, who):
        return None                         # 塞得下 = 没被压住
    opened = terrain.variant(())
    if opened is terrain or not botmove.fits(opened, body.x, body.y, who):
        return None                         # 拿掉破坏物照样塞不下 = 是地形
    # 到这儿已经证明「压住我的是破坏物」，剩下的只是挑哪一件：最近的。
    return min((item for item in terrain.breakables if item.index in alive),
               key=lambda item: item.distance_to(body.x, body.y),
               default=None)


def _breaking_now(room, machine, seat_index, terrain=None):
    """这一刻该不该把火力压在挡路物上 —— 那一件，或者 `None`。

    用户要的是「挡路就打」，**不是**「人贴脸了也先打罐子」。所以
    锁定了挡路物之后还要过一道现成的门：

    * 被它**压住**（`_breakable_pinning_body`）—— 无条件打，不打碎
      它连挪都挪不动，这正是用户报的「被围住困在里面」；
    * ★★★★★ **不打碎它就哪儿都去不了**（`path_breakable_only`，V0.3 §172）
      —— 同一类事实，同一条待遇。后台在**完整地形**上连「往目标挪近一格」
      都规划不出来时置起。少了这一条，下面那道「有敌人就先不打」的门会
      让 bot **站在原地一动不动**：走不了（唯一的路被罐子堵着）、又不许打
      （视野里有敌人），而它不动 ⇒ 规划签名不变 ⇒ 永远不重算。实机
      `CamelCulvert04` bot1 在 (1285, 853) 站了 **27 秒**，直到真人自己
      挪窝把目标点带走才解开 —— 用户 2026-09-02：「一进游戏所有 bot 都
      不动，必须我动了之后 bot 才开始动。」
    * 否则**视野里有活敌人 / 活怪**就先不打 —— 走位和开火两边用的是
      同一句判断，不会出现「走位站住等打罐子、开火却去打人」的僵局。

    判据全是现成的表（`_visible_targets` / `live_mobs` + `_in_sight`），
    不引入第二套「威胁」口径。
    """
    terrain = _terrain(room) if terrain is None else terrain
    item = _path_breakable_item(room, machine, terrain)
    if item is None:
        return None
    if _breakable_pinning_body(machine, terrain) is not None:
        return item
    if machine.path_breakable_only:
        return item
    if _visible_targets(room, machine, seat_index):
        return None
    if room.team_layout() == lobby_module.TEAM_LAYOUT_COOP:
        for mx, my, _handle in live_mobs(room):
            if _in_sight(machine, mx, my):
                return None
    return item


def _breakable_move_intent(room, machine, seat_index, terrain, target):
    """已锁定捷径的第一道破坏物时，走安全前缀或站住开火。"""
    if _breaking_now(room, machine, seat_index, terrain) is None:
        return None
    if target is not None and target[0] == BREAKABLE_SEAT:
        return (0, False, False, False)       # `_tick_bot` 后面照常扣扳机
    if machine.nav_path and machine.nav_goal is not None:
        routed = _route_intent(machine, terrain, _character_of(machine),
                               machine.nav_goal, hold_at_breakable=True)
        if routed is not None:
            return routed
    # ★ 安全前缀走完了，可这一枪又不是冲着它去的（够不着 / 换枪还没生效）
    #   —— **不站在那儿干等**，放行给正常走位，下一轮后台规划会重新给
    #   答案。这条是「站住打挡路物」和「一动不动」之间唯一的分界。
    return None


def _move_intent(room, machine, seat_index, terrain, target, now=None):
    """这一帧往哪走 —— 返回 `(方向, 起跳, 下落, 冲刺跑)`。

    ## 规则只有两条，而且都不是我发明的（D50 的口径）

    1. **打得到就打**：`_fire_target()` 挑出了目标 = 这一枪值得扣扳机，
       那就站住打。语料里真人 39% 的心跳是站着不动的 —— 站着开枪是常态。
    2. **打不到就走过去**：没得打的时候朝最近的敌人挪。出处是 §48 那份
       距离分布 —— 真人打中人的距离 p10=264 / 中位 616 / p99=1015，
       而地图宽 1500~2848 ⇒ **真人是主动靠过去的**，不会隔着半张图对望。

    地形只回答「这一步走不走得成」，不添新规则：

    * 前面是爬不上去的坎 → **跳**（跳得上去就上去，跳不上去就在那儿蹦，
      真人卡在墙根时也是这样）；
    * 前面是掉不到底的坑 → 先看**跳过去**行不行（`jump_lands`），
      不行就不往里走。⚠ 这不是「保持距离」那种规矩，是「脚下有没有路」。

    高台 / 坑 / 上下层不再靠“原地反复跳”兜底：M5-B 交给
    `botnav.plan()`，它用这同一套 `botmove` 物理懒生成可达边并跑 A*。
    提前量、战术性冲刺和蹲仍在后续阶段。
    """
    body = machine.body
    # ★ 诊断（§167）：这一格的走位目标由 `_walk_to()` 填；先清掉，
    #   免得某条不经过它的分支把上一格的目标当成自己的打出来。
    machine.walk_goal = None
    if body is None or terrain is None:
        return _src(machine, "没身体/没地形", (0, False, False, False))
    # ★★★★ **被破坏物压住排在脱困之前**：`LockInBreakable=1` 的图会把
    #   人塞进罐子掩码里，那一刻 `_unstick_intent()` 也没有方向可走
    #   （裹着人的是实心掩码，往哪挪都是塞不下），唯一的出路是把它打碎。
    #   判据见 `_breakable_pinning_body()`，同样是一句 `fits()` 的地形差。
    pinned = _breakable_pinning_body(machine, terrain)
    if pinned is not None:
        if machine.path_breakable != pinned.index:
            # ★ 按**状态翻转**记（铁律 10）：锁的是哪一件变了才重置路线。
            machine.path_breakable = pinned.index
            machine.path_breakable_prefix = []
            machine.path_breakable_only = False
            _clear_navigation(machine)
            machine.log(f"   被可破坏物 {pinned.handle} 压住 -> 站住打碎它")
        if target is not None and target[0] == BREAKABLE_SEAT:
            # 已经瞄上它了：站住打
            return _src(machine, "被压住·站住打它", (0, False, False, False))
        # 还没瞄上它（这一格才刚锁上），或者手上这把枪根本够不着 ——
        # 那就照旧往下走 `_unstick_intent()`，挪出去也是一条出路。
    # ★★★★ **卡在缝里排在一切之前**（V0.3 §152）：塞不进去的时候躲子弹、
    #   打怪、跟队伍全都无从谈起 —— 人根本挪不动。先出去再说。
    #   没卡住时它只花一次 `fits()`（15 µs），等于没开销。
    stuck = _unstick_intent(room, machine, terrain)
    if stuck is not None:
        return _src(machine, "脱困", stuck)
    coop = room.team_layout() == lobby_module.TEAM_LAYOUT_COOP
    # ★★★★ **牵引绳排在最前面**（D99）：闯关时落后带头的真人超过 1/4 个
    #   屏幕就无条件追，连躲子弹都往后排 —— 掉队的那一个会把整队的扇区
    #   进度钉死，比挨几枪严重得多。没掉队时它返回 None，下面照旧。
    #   ★ boss 房里它**整条不生效**（§141）：那儿没有进度可推，
    #   bot 只管打 boss（用户 2026-08-30 第四轮实机）。
    if coop and not _boss_room(room):
        leash = _coop_leash_intent(room, machine, seat_index, terrain)
        if leash is not None:
            return _src(machine, "牵引绳", leash)
    else:
        _leash_release(machine)
    # ★★★ **躲子弹排在一切之前**（M5-E）：真人也是先躲开再想别的。
    #   躲得开就按躲的那套键走这一 tick，路线作废（人已经被挪到别处了）。
    dodge = _dodge_intent(room, machine, seat_index, terrain,
                          _now() if now is None else now)
    if dodge is not None:
        _clear_navigation(machine)
        return _src(machine, "躲子弹", dodge)
    # ★★★ **打通挡路的破坏物**（用户 2026-09-01）：后台双路线比较证明
    #   打碎它的那条路更短时，走完安全前缀就站住开火。
    #   ★ 排在脱困 / 牵引绳 / 躲子弹**之后**：那三条各自都是「不先做这件事
    #     别的都无从谈起」，而罐子不会跑 —— 躲完这一发再打完全来得及。
    #     真被裹在里面的那种「非打不可」已经在上面单独处理过了。
    breaking = _breakable_move_intent(room, machine, seat_index, terrain,
                                      target)
    if breaking is not None:
        return _src(machine, "破障", breaking)
    # ★★★ 闯关房（M5-G）：那儿没有「敌方座位」，走位的目标是**跟上队伍**。
    if coop:
        intent = _coop_intent(room, machine, seat_index, terrain, target)
        return _src(machine, "闯关", intent, goal=machine.walk_goal)
    enemy = _enemy_spot(room, machine, seat_index)
    if enemy is None:
        # ★★★ 视野里一个敌人都没有（§127 / §128）—— 这时候**不站着发呆**。
        intent = _blind_intent(room, machine, seat_index, terrain)
        return _src(machine, "框外走位", intent, goal=machine.walk_goal)
    enemy_index, spot = enemy
    enemy_span = math.hypot(spot[0] - body.x, spot[1] - body.y)
    # ★★★ 逼近还是拉开（M5-C）—— 用户 2026-08-29 要的那条「按双方血量判断」。
    #     判据在 `_stance()`：照这样打下去谁先倒。
    fast_run = False
    if _stance(room, machine, seat_index, enemy_index,
               enemy_span) == "retreat":
        goal = machine.retreat_goal
        if goal is None or _retreat_done(body, goal, spot):
            goal = _retreat_spot(room, machine, terrain, spot)
            machine.retreat_goal = goal
            _clear_navigation(machine)
        if goal is None:
            # 退无可退（图边 / 没有落脚点）——那就照旧打，别原地发呆。
            #
            # ★★★ **不改写姿态闩**（V0.3 §165）。以前这儿写一句
            #   `machine.stance = "press"`，可「退无可退」是**地形**事实，
            #   不是战况判断变了 —— 下一格 `_stance()` 拿同一组血量 / 输出
            #   一算，照旧说「该退」，于是闩每一格翻一次，两道迟滞门
            #   （`BOT_RETREAT_RATIO` / `BOT_PRESS_RATIO`）形同虚设。
            #   闩留在 `retreat` 上，回 `press` 就得真的「明显更占便宜」，
            #   这才是那两个数当初的意思。走位这一格照旧朝敌人走，一个字没变。
            pass
        else:
            spot = goal
            # ★ 脱离时按着右键跑（`FastRunRate`）—— 体力够才跑，
            #   这是原版的开关，不是我们加的动作。
            fast_run = _may_fast_run(machine)
    else:
        machine.retreat_goal = None
        # ★★ **主动捡道具**（M5-F）：这一枪打不出去（`target is None`）的时候
        #    本来就是要朝敌人挪的，那就先绕去把路上比敌人更近的那件捡了。
        #    ⚠ 打得到人的时候**不去捡** —— 真人也不会当着枪口去蹲地上那件。
        item = None if target is not None else _item_goal(room, machine,
                                                          seat_index)
        if item is not None and item[0] < enemy_span:
            spot = item[1]
        elif target is not None:
            # 打得到就站住打（老规则，D50）。★ 只在**逼近**姿态下成立：
            #   拉开的时候真人也是一边退一边打的。
            _clear_navigation(machine)
            return _src(machine, "打得到·站住打", (0, False, False, False))
    return _src(machine, "朝目标走",
                _walk_to(room, machine, terrain, spot, fast_run), goal=spot)


def _blind_cover_spot(machine, terrain, bearing):
    """只拿“上/下/左/右”粗方向找一个掩体点。

    候选仍是原版地形的站立面；“躲住了没有”仍用
    `line_blocked()`（单向平台不挡子弹）。有掩体就选离自己最近的，
    一处都没有就选离粗方向最远的可站点；精确敌人坐标自始至终
    没有进入这个函数。
    """
    body = machine.body
    if body is None or terrain is None or bearing is None:
        return None
    who = _character_of(machine)
    # 已经在掩体后面，当前点就是最近答案。
    mx, my = _muzzle(body.x, body.y, bearing[0])
    if terrain.line_blocked(mx, my, bearing[0],
                            bearing[1] - BOT_MUZZLE_HEIGHT):
        return (body.x, body.y)
    reach_y = (botmove.walk_speed(who) * botmove.CLIMB_SLOPE
               * botmove.TICKS_PER_BEAT)
    covers = []
    fallback = []
    steps = max(1, int(BOT_RETREAT_SPAN / BOT_RETREAT_STEP))
    for index in range(1, steps + 1):
        for direction in (-1, 1):
            cx = body.x + direction * BOT_RETREAT_STEP * index
            if cx < 0 or cx >= terrain.width:
                continue
            sy = botmove.surface_near(terrain, cx, body.y, reach_y)
            if sy is None or not botmove.fits(terrain, cx, sy, who):
                continue
            point = (float(cx), float(sy))
            from_here = math.hypot(point[0] - body.x, point[1] - body.y)
            from_bearing = math.hypot(point[0] - bearing[0],
                                      point[1] - bearing[1])
            px, py = _muzzle(point[0], point[1], bearing[0])
            if terrain.line_blocked(px, py, bearing[0],
                                    bearing[1] - BOT_MUZZLE_HEIGHT):
                covers.append((from_here, -from_bearing, point))
            fallback.append((from_bearing, -from_here, point))
    if covers:
        return min(covers, key=lambda row: (row[0], row[1], row[2]))[2]
    if fallback:
        return max(fallback, key=lambda row: (row[0], row[1], row[2]))[2]
    return None


def _blind_probe_intent(room, machine, terrain, side):
    """框外走位算出「一动不动」时，**横着探一步**；无处可走返回 `None`。

    竖直粗方位（`(自己的 x, y ± 视野高)`）在 `_walk_to()` 里 `direction`
    恒为 0，A\\* 再解不出上下层的路就是原地发呆 —— 用户 2026-09-01：
    「尽量不要傻站着不动」。真人知道人在楼上时也是**横着走去找上去的
    那条路**，不是杵在原地仰头。

    方向先取敌人那一侧（只有符号，不含距离），那一侧撞墙 / 前面是
    无底洞就换另一侧；两侧都不成才是真的没有合法动作。
    """
    body = machine.body
    if body is None or terrain is None or not body.on_ground:
        return None
    who = _character_of(machine)
    crouched = bool(machine.dodge_crouch)
    scale = _speed_scale(machine, _now())
    first = side if side else 1
    for direction in (first, -first):
        if botmove.blocked(terrain, body, who, direction, fast_run=False,
                           crouched=crouched, speed_scale=scale):
            continue
        if botmove.bottomless_ahead(terrain, body, who, direction,
                                    fast_run=False, crouched=crouched,
                                    speed_scale=scale,
                                    ticks=BOT_DECISION_TICKS):
            continue
        return (direction, False, False, False)
    return None


def _blind_walk(room, machine, terrain, spot, side, fast_run):
    """框外走位专用的 `_walk_to()`：算出来一动不动就横着探一步。"""
    intent = _walk_to(room, machine, terrain, spot, fast_run)
    if intent[0] or intent[1] or intent[2]:
        return intent
    probe = _blind_probe_intent(room, machine, terrain, side)
    return intent if probe is None else probe


def _src(machine, tag, intent, goal=None):
    """★ 诊断（V0.3 §164）：记下这一帧的走位意图是**哪个分支**给的。

    ★★ `goal` 是这一格走位**朝哪个点**（V0.3 §167 补的）。会话 54 查
    §165 时在这上面白花了一整轮：`_src()` 只说了分支，而同一个分支的
    `spot` 可能是敌人 / 道具 / 后撤点 / 跟随点，光看标签分不出来。
    它**不进去重键**（目标每格都在动，进了键就等于不去重），只在
    「分支或方向真的翻了」那几行上顺手打出来。

    用户 2026-09-02 00:48：「bot 走路还会有一卡一卡的感觉……来回跳。」
    心跳里量出来的形态很干净 —— bot 贴在地图横向边缘上，位置以
    **3 发心跳（12 tick）为周期**在两三个点之间来回，幅度 7~14 像素：

        seat 2  00:48:19.6~23.6  x = 0 → 4 → 7 → 0 → 4 → 7 …（图左边缘）
        seat 3  00:48:09.7~10.4  x = 2043 → 2040 → 2036 → 2043 …（图右边缘）

    纯物理排除掉了：`botmove.tick(direction=-1)` 在 x=0 处**一格都不动**；
    起跳的话 y 会从 258 冲到 101（心跳里没有，`on_ground` 全程是 1）。
    ⇒ 来回跳的是**意图**，不是物理：两个分支在互相打架。这一行就是
    「到底是哪两个」。

    按状态翻转去重（铁律 10）：分支或方向变了才打一行。
    """
    if intent is None:
        return intent
    key = (tag, intent[0], bool(intent[1]), bool(intent[2]))
    if key != machine.diag_src:
        machine.diag_src = key
        where = ("" if goal is None
                 else f" 目标=({goal[0]:.0f}, {goal[1]:.0f})")
        body = machine.body
        mine = "" if body is None else f" 我在=({body.x:.0f}, {body.y:.0f})"
        machine.log(f"   ◆走位来自「{tag}」 方向={intent[0]:+d} "
                    f"跳={'是' if intent[1] else '否'} "
                    f"下={'是' if intent[2] else '否'}{where}{mine}")
    return intent


def _note_idle(machine, why):
    """★ 诊断：这个 bot 为什么**一动不动**（V0.3 §162）。`None` = 它在动。

    用户 2026-09-01 已经报过三回「所有 bot 一起卡住」。心跳里能看出「站着」，
    看不出「为什么」—— 这一行就是那个「为什么」。

    **按状态翻转去重**（铁律 10 的口径）：理由和上一次一样就什么都不打，
    真的换了才打一行。所以 32 ms 一格的节奏不会把日志刷爆，
    而「从这一刻起站住了 / 又开始动了」两个时刻都看得见。
    """
    if why == machine.diag_idle_why:
        return
    first = machine.diag_idle_why == ""      # 还没记过任何一次（哨兵）
    machine.diag_idle_why = why
    if why is not None:
        machine.log(f"   ◆一动不动：{why}")
    elif not first:
        machine.log("   ◆又动起来了")


def _blind_intent(room, machine, seat_index, terrain, spawn_fallback=True,
                  may_hide=True):
    """**视野里没有敌人**的那一帧往哪走（V0.3 §127 / §128）。

    两种情形，用户 2026-08-30 各报了一条：

    1. 敌人还活着，只是在屏幕外 ——「可以告诉 bot 敌人的粗略方位，上下左右
       这个粗略程度就够了」。⇒ 朝 `_rough_bearing()` 给的那个**视野边缘上的
       点**挪，挪到人进了视野框就自动切回精确瞄准。
    2. 敌人全躺着 ——「敌人死后 bot 就停下不动了。我希望改成像真人一样，
       会自己走位，提前寻找有利地形。」⇒ 朝**敌方的出生点**挪。

    ★ 第 2 条为什么是「敌方出生点」而不是别的什么「有利地形」（铁律 11）：
      出生点是**原版地图数据里就有的东西**（`terrain.points`，和真人重生
      走的是同一张表），而「对方一定会从那儿站起来」是这一局的硬事实。
      提前占住那儿是有依据的走位，不是我们发明的战术评分。

    ★ 道具模式里地上的东西照旧优先（`_item_goal`）—— 没人打的时候正是
      去捡道具的时候，这条本来就在（M5-F），只是以前这一帧根本走不到。
    """
    body = machine.body
    if body is None:
        _clear_navigation(machine)
        return (0, False, False, False)
    rough = _rough_bearing_raw(room, machine, seat_index)
    if rough is not None:
        spot, side = rough
        health = _seat_health(room, seat_index)
        if not may_hide:
            # ★ boss 房不许躲（`_boss_fight_intent` 传的）：那儿的门要打死
            #   boss 才开，两个 bot 一起躲起来就是把这一关钉死（同 §141
            #   「boss 房里没有推进进度这回事」的口径）。
            machine.stance = "press"
            machine.retreat_goal = None
        elif health < BOT_BLIND_HIDE_BELOW:
            machine.stance = "retreat"
            machine.retreat_goal = None
        elif health > BOT_BLIND_PRESS_ABOVE:
            machine.stance = "press"
            machine.retreat_goal = None
        # 25%..50% 一字不动：保持上一姿态，这就是滞回。
        if may_hide and machine.stance == "retreat":
            goal = machine.retreat_goal
            if goal is None or _retreat_done(body, goal, spot):
                goal = _blind_cover_spot(machine, terrain, spot)
                machine.retreat_goal = goal
                _clear_navigation(machine)
            if goal is not None:
                if (abs(goal[0] - body.x) <= botnav.GOAL_X
                        and abs(goal[1] - body.y) <= botnav.GOAL_Y):
                    # 已经蹲在掩体后面了 —— 这时候站住不动**就是**
                    # 正确动作，别拿探路把自己从掩体后面赶出去。
                    _clear_navigation(machine)
                    _note_idle(machine, "躲在掩体后面（血 %.0f%%）" % (health * 100))
                    return (0, False, False, False)
                _note_idle(machine, None)
                return _blind_walk(room, machine, terrain, goal, -side,
                                   _may_fast_run(machine))
            # 真没有任何可站候选才允许停；这是“无合法动作”，
            # 不是视野空了就发呆。
            _clear_navigation(machine)
            _note_idle(machine, "低血量想躲，可一处可站的掩体候选都没有"
                                "（血 %.0f%%）" % (health * 100))
            return (0, False, False, False)
        _note_idle(machine, None)
        machine.retreat_goal = None
        item = _item_goal(room, machine, seat_index)
        if item is not None and item[0] < math.hypot(spot[0] - body.x,
                                                     spot[1] - body.y):
            spot = item[1]
            side = (0 if abs(spot[0] - body.x) < 1.0
                    else int(math.copysign(1, spot[0] - body.x)))
        return _blind_walk(room, machine, terrain, spot, side, False)

    # 一个活敌人都没有：PVP 去敌方出生点等；任务模式由
    # `_coop_intent` 照带头真人/活动带继续处理，不凭空发明巡逻点。
    # ★ 姿态复位放在这里（以前在 `_move_intent` 调用点上）：没有敌人
    #   就无所谓逼近/后撤，留着上一轮的 `retreat_goal` 只会让人绕远。
    machine.stance = "press"
    machine.retreat_goal = None
    if not spawn_fallback:
        return None
    item = _item_goal(room, machine, seat_index)
    spot = _spawn_watch_spot(room, machine, seat_index, terrain)
    if item is not None and (spot is None
                             or item[0] < math.hypot(spot[0] - body.x,
                                                     spot[1] - body.y)):
        spot = item[1]
    if spot is None:
        _clear_navigation(machine)
        return (0, False, False, False)
    return _walk_to(room, machine, terrain, spot, False)


def _spawn_watch_spot(room, machine, seat_index, terrain):
    """敌人全躺着时去哪儿等 —— **离自己最近的那个敌方出生点**。

    ★ 出生点表是原版地图自带的（`mapdata` 从 `.map` 里抽的 `points`，
    真人重生走的也是它）。走到那儿不是「我们发明的有利地形」，
    是「对方一定会从这儿起来」这个事实。
    """
    if terrain is None or machine.body is None:
        return None
    mine = _spawn_team(room, seat_index)
    # 组队房去**对面那一边**；个人战一律传 0，`_spawn_points()` 会把
    # 101 + 102 拼成一张全表（和原版 `0x473ba8` 同一条分支）。
    enemy_team = {1: 2, 2: 1}.get(mine, 0)
    best = None
    for px, py in _spawn_points(terrain, enemy_team):
        span = math.hypot(px - machine.body.x, py - machine.body.y)
        if best is None or span < best[0]:
            best = (span, (float(px), float(py)))
    return None if best is None else best[1]


def _walk_to(room, machine, terrain, spot, fast_run):
    """朝 `spot` 走一步 —— 返回 `(方向, 起跳, 下落, 冲刺跑)`。

    地形只回答「这一步走不走得成」，不添新规则；走不成就交给
    `botnav.plan()`（M5-B），A\* 也找不到才退回墙根跳 / 坑前停的老兜底。
    """
    body = machine.body
    machine.walk_goal = (float(spot[0]), float(spot[1]))   # ★ 诊断（§167）
    delta_x = spot[0] - body.x
    direction = 0 if abs(delta_x) < 1.0 else (1 if delta_x > 0 else -1)
    who = _character_of(machine)
    # ★★ 预测要和**执行**用同一组参数（V0.3 §151）：`_own_step()` 真跑这一格
    #    时带的就是这两个，而这里以前一个都不带 —— 被冻住（倍率 0.0）或者
    #    踩了减速胶水（0.3）的 bot 会按满速算出「跳得过去」，然后原地竖直
    #    跳进坑里。起跳带走的是**这一刻的走速**（§93），差一档就差整条弧线。
    crouched = bool(machine.dodge_crouch)
    scale = _speed_scale(machine, _now())
    if not body.on_ground:
        # ★★ 腾空时方向键**改不了**水平速度（§93 推翻了 §71 的那一行）——
        #   这里照样返回方向，只是为了这一批 tick 里**落地之后**那几个 tick
        #   接着往前走；空中那几个 tick 它是死的。
        if machine.nav_path:
            routed = _route_intent(machine, terrain, who, spot)
            if routed is not None:
                return routed
        # ★★★ **兜底跨坑那一跳的第二段**（§144）：A\* 还没算完 / 说不可达时
        #   走的是下面那条老兜底，它起跳时把「这一段要补第二跳」记在
        #   `nav_double_jump` 上。判据和规划层同一句（`botmove.at_apex`）。
        again = machine.nav_double_jump and botmove.at_apex(body)
        return (direction, again, False, fast_run)

    # ★ 踩着地 = 上一段腾空结束，兜底那一跳的「补第二段」旗子作废。
    #   放在这里而不是落地事件上：`_route_intent` 走 A\* 路线时会**自己**
    #   按 `Step.double` 重设它，清早了也不影响（§144）。
    machine.nav_double_jump = False
    # ★ 已经有路线时先执行路线，**不再算一遍 blocked / bottomless** ——
    #   那两个判据各要跑一遍物理模拟，而逐 tick 决策（`_own_step`）会把这个
    #   函数调用频率提高到每 tick 一次。有路线时它们的结论也用不上。
    if machine.nav_path:
        routed = _route_intent(machine, terrain, who, spot)
        if routed is not None:
            return routed

    blocked = botmove.blocked(terrain, body, who, direction, fast_run=fast_run,
                              crouched=crouched, speed_scale=scale)
    # ★★★ 前瞻要覆盖**这份意图的寿命**（V0.3 §151）：意图是 `_decide()`
    #   `BOT_DECISION_TICKS` 格产出一次的，产出之后要握着用那么久；而崖边
    #   「下一步就踩空」的窗口在真图上只有一个走步宽（`Quest02_1#Normal`
    #   实测 8~11 像素）。只问一格的话约一半的接近位置整个跳过这个窗口 ——
    #   实测掉坑率 50%，改成按寿命前瞻之后 0.1%。
    #   ★ 决策频率一个字没动（§146 里用户明确否掉过「就地重问」），
    #     改的是**看多远**。
    bottomless = botmove.bottomless_ahead(
        terrain, body, who, direction, fast_run=fast_run, crouched=crouched,
        speed_scale=scale, ticks=BOT_DECISION_TICKS)
    vertical = abs(spot[1] - body.y) > botnav.GOAL_Y
    # ★★★★★ **「前面那一步塞不进去」也得先问 A\***（V0.3 §171）。
    #
    #   这一条以前只写在下面的兜底里：塞不进去就回「不动」。可是
    #   `blocked` / `bottomless` / `vertical` 三条都是假的（前面有地、
    #   不是坑、目标不在上下层），于是**规划单一张都不递** —— bot 就在
    #   原地站到天荒地老。实机 `CamelCulvert04` 13:21:11 起 bot1 在
    #   (279, 990) 站了 **87 秒**（bot2 56 秒、bot3 58 秒），到局终都没动：
    #   往左第一步落在 (271, 990)，那儿被 48 号罐子挤成一条塞不下的缝。
    #
    #   而 A\* 对这种局面是有答案的：完整地形上 `reached=False`，
    #   「假定罐子全碎」的地形上**一步就到**，`first_breakable_on_path()`
    #   认得出挡路的是 48 号 ⇒ 该做的事是**把它打碎**，不是站着。
    #   ⇒ 把它并进「该问路的四种情形」。问不出来才落到下面那条兜底。
    #
    #   ★ 和 §160 是同一个型：那次是 `hold_at_breakable` 把规划挡在门外，
    #     这次是缝。症状也一样 ——「所有 bot 都不动，过一阵又都恢复」。
    crack = bool(direction) and _walks_into_a_crack(
        terrain, body, who, direction, fast_run, crouched, scale)
    if blocked or bottomless or vertical or crack:
        routed = _route_intent(machine, terrain, who, spot)
        if routed is not None:
            return routed
    else:
        _clear_navigation(machine)

    # A* 找不到才退回旧 fail-safe：墙根尝试跳，真正的无底洞前停下。
    if blocked:
        return (direction, True, False, fast_run)
    if bottomless:
        # 脚下这一步是个掉不到底的坑：跳得过去就跳，跳不过去就别走。
        # ★★★ 一段跳够不着时**再问一次二段跳**（§144）—— 用户 2026-08-30：
        #   「经过岩浆时，bot 似乎不会用二段跳来跳到对面平台，只会用一段跳，
        #   然后反复掉进岩浆。」二段跳在 A\* 那边一直是有边的（§124），
        #   缺的是**这条兜底路**：A\* 还没算完（后台线程，§137）或者说
        #   「到不了」的时候走的就是这里，而它以前只会一段跳。
        # ★ 两次模拟都带上 `fast_run`：起跳带走的是**那一刻的走速**（§93），
        #   冲刺着跳比走着跳远得多，不带的话预测的落点根本不是真落点。
        if _landing_ok(terrain, _jump_lands(terrain, body, who, direction,
                                            fast_run, crouched, scale), who):
            return (direction, True, False, fast_run)
        if _landing_ok(terrain,
                       _double_jump_lands(terrain, body, who, direction,
                                          fast_run, crouched, scale), who):
            machine.nav_double_jump = True
            return (direction, True, False, fast_run)
        return (0, False, False, False)
    if vertical and spot[1] < body.y:
        # ★★★ **目标在上面 ≠ 现在就该跳**（§146）。这条兜底原来是无条件
        #   起跳的，而跟随点只要比自己高一点（对岸台子高 75 就够）它就在
        #   **离坑还有 150 的平地上**起跳 —— 弧线飞到对岸时早就低于台面了，
        #   一头栽进岩浆。用户 2026-08-30：「经常跳早了，导致跳不过去。」
        # ⇒ 先问一句「这一跳落得住吗、落了之后是不是真的更高」：落不住
        #   （掉出图外 / 掉进坑）或者白跳，就**接着走** —— 走到坎底下再蹦，
        #   走到坑边上会有 `bottomless` 那两条接手。
        landing = _jump_lands(terrain, body, who, direction,
                              fast_run, crouched, scale)
        if _landing_ok(terrain, landing, who) and landing.y < body.y - 1.0:
            return (direction, True, False, fast_run)
        # ★★ 一段够不着就问二段（V0.3 §151）—— `bottomless` 那条从 §144
        #    起就有这一句，`vertical` 这条一直漏着：目标平台高过一段跳的
        #    顶点（167）而 A\* 又还没算完时，bot 只会在下面一段跳，上不去。
        landing = _double_jump_lands(terrain, body, who, direction,
                                     fast_run, crouched, scale)
        if _landing_ok(terrain, landing, who) and landing.y < body.y - 1.0:
            machine.nav_double_jump = True
            return (direction, True, False, fast_run)
    if crack:
        # ★ 前面那一步碰撞体塞不进去（V0.3 §152）—— 别往里蹭。站住比卡住好。
        #   ★★ 但**先问过 A\* 了**（上面那一段，§171）：这里是「连路都没有」
        #   才走到的最后一步，不是第一反应。
        return (0, False, False, fast_run)
    # ★★★★★ **走不满一步的零头，不值得掉头**（V0.3 §167）。
    #
    #   这一层是个 bang-bang 控制器：方向只有 `±1`，而**一份意图要握着走
    #   `BOT_DECISION_TICKS` 个 tick**（`_own_step()` 逐格消费同一个
    #   `machine.intent`）。上面那句 `abs(delta_x) < 1.0` 的容差是 1 像素,
    #   一次决策却走 14~21 像素 ⇒ 只要目标点落在 bot 迈得过去的地方，
    #   它就**必然**踩过头、下一格再踩回来，15 Hz 一个来回、幅度一整步。
    #
    #   实机 `Esperan00` 10:18:50~52（`logs/server.out`，10:14:45 那次启动）
    #   离线原样复现：目标 x=781，bot 在 793 ↔ 779 之间来回，
    #   `◆走位来自「朝目标走」` 每 62 ms 翻一次，心跳里看到的就是
    #   「位置来回微微跳」。用户 2026-09-02 第二轮实机报的就是它。
    #
    #   ⇒ 判据不是新拍的常数，是**这份意图的寿命里能走多远**（和 §151 那条
    #     前瞻用的是同一把尺）：要掉的这个头如果只是为了修一个比它还短的
    #     零头，那就站住。目标真的挪到一步以外时照旧掉头，一个字没变。
    #
    #   ★ 只挡**掉头**，不挡「接着往前走」：所以「走过去踩到那一点」这件事
    #     一次都没少做（穿过去捡道具照旧），少掉的只有踩过头之后的那次折返。
    if direction and machine.walk_last == -direction:
        stride = botmove.walk_speed(who, fast_run=fast_run,
                                    crouched=crouched,
                                    scale=scale) * BOT_DECISION_TICKS
        if abs(delta_x) <= stride:
            return (0, False, False, fast_run)
    if direction:
        machine.walk_last = direction
    return (direction, False, False, fast_run)


def _jump_lands(terrain, body, who, direction, fast_run, crouched, scale):
    """`botmove.jump_lands()` 的短名字版 —— 参数照 `_own_step()` 那一组带全。"""
    return botmove.jump_lands(terrain, body, who, direction,
                              fast_run=fast_run, crouched=crouched,
                              speed_scale=scale)


def _double_jump_lands(terrain, body, who, direction, fast_run, crouched,
                       scale):
    """同上，二段跳那一份。"""
    return botmove.double_jump_lands(terrain, body, who, direction,
                                     fast_run=fast_run, crouched=crouched,
                                     speed_scale=scale)


def _landing_ok(terrain, landing, who):
    """这个落点**落得住**、而且碰撞体**塞得下**（V0.3 §152）。

    以前只问前半句。后半句是 `Iceria03` (1174, 864) 那条 6 像素宽的冰缝
    教的：落点在缝里，服务端的点模型觉得没问题，客户端的三个碰撞圆把人
    卡死在缝口 —— 实机里两个 bot 先后卡在同一个像素上，一个 59 秒一个 13 秒。
    """
    return (landing is not None
            and botmove.fits(terrain, landing.x, landing.y, who))


def _walks_into_a_crack(terrain, body, who, direction, fast_run, crouched,
                        scale):
    """照这个方向走一步，会不会踩进一条**塞不进去**的缝（V0.3 §152）。"""
    step = botmove.tick(terrain, body, who, direction=direction,
                        fast_run=fast_run, crouched=crouched,
                        speed_scale=scale)
    if not step.on_ground or step.x == body.x:
        return False                   # 踩空/撞墙自有上面那两条判据管
    return not botmove.fits(terrain, step.x, step.y, who)


def _unstick_intent(room, machine, terrain):
    """★★★ 已经卡在塞不进去的地方了 —— 往外挪（V0.3 §152）；没卡返回 `None`。

    ## 为什么要有这一条

    `_landing_ok()` 只挡「主动往里跳」。被手雷炸飞、被击退、A\\* 缓存还是
    旧的 —— 人照样可能落进缝里。而**对战模式一条脱困都没有**：
    `_coop_leash_intent()` 那套被 `if coop` 挡在闯关模式里，量的还是
    「推进进度」；`Iceria` 也不在 `FallDown` 名单里，掉进去既不判死也没人捞。
    实机里那两个 bot 就这么杵了 59 秒和 13 秒。

    ★ 判据是**几何事实**（碰撞体塞不塞得下），不是「多久没动」这种计时器
      （铁律 10）。没卡住的时候只花一次 `fits()`（实测 15 µs）。

    出去的路按**真跑一遍**挑，和别处一个口径：先看走得出去吗，走不出去
    就问跳，两段都试；实在没辙就朝净空宽的那一侧跳一下，总比杵着强。
    """
    body = machine.body
    if body is None or terrain is None or not body.on_ground:
        return None
    who = _character_of(machine)
    if botmove.fits(terrain, body.x, body.y, who):
        return None
    # 路线是照着「点模型」算出来的，而这会儿已经证明那套模型在这儿不成立。
    # ★ 排在挂旗子前面：`_clear_navigation()` 踩地时会把 `nav_double_jump` 清掉。
    _clear_navigation(machine)
    for direction in _unstick_directions(terrain, body, who):
        # 走：一条边的长度（`botnav.WALK_TICKS`）之内能走到塞得下的地方吗。
        step = body
        for _ in range(botnav.WALK_TICKS):
            step = botmove.tick(terrain, step, who, direction=direction)
            if not step.on_ground or step.x == body.x:
                break
            if botmove.fits(terrain, step.x, step.y, who):
                return (direction, False, False, False)
    for direction in _unstick_directions(terrain, body, who):
        if _landing_ok(terrain, botmove.jump_lands(terrain, body, who,
                                                   direction), who):
            return (direction, True, False, False)
        if _landing_ok(terrain, botmove.double_jump_lands(terrain, body, who,
                                                          direction), who):
            machine.nav_double_jump = True
            return (direction, True, False, False)
    # 都不成：朝宽的那一侧跳。缝里跳一下至少能换个落点，杵着永远不会变。
    return (next(iter(_unstick_directions(terrain, body, who)), 1),
            True, False, False)


def _unstick_directions(terrain, body, who):
    """脱困先往哪边试 —— **净空宽的那一侧优先**，然后是另一侧。"""
    radius = float(getattr(who, "size_body", 13.0) or 13.0)
    reach = int(round(radius * 4.0))
    y = int(body.y - radius)
    room_right = room_left = 0
    while room_right < reach and not terrain.is_solid(int(body.x) + room_right + 1, y):
        room_right += 1
    while room_left < reach and not terrain.is_solid(int(body.x) - room_left - 1, y):
        room_left += 1
    return (1, -1) if room_right >= room_left else (-1, 1)


#: 客户端视口的宽（世界单位）。`ViewPort::Init(x, y, w, h)` = `0x5cc7f5`，
#: 全镜像唯一构造点 `0x5bfc8f` 传的是 `(0, 0, 0x400, 0x300)` ⇒ **1024 × 768**。
#: （夺分模式那条「距离 > 视口宽就减伤」用的也是同一个 `[视口+0x30]`，§89。）
BOT_VIEWPORT_WIDTH = 1024.0

#: ★★★ **闯关模式的牵引绳**：bot 沿前进轴落后带头的真人**不许**超过这么远。
#:
#: 用户 2026-08-30：「给 bot 加一条优先级最高的指令，无论发生什么，不可以离
#: 最前面的真人距离超过 3/4 个屏幕的距离，超过 3/4 个屏幕的距离就要无条件
#: 向前追上最前面的人，实在走不过去就瞬移传送也行。」
#:
#: ⚠ 这一条**不是从原版推出来的**（铁律 11 的例外，D99）—— 它是用户在
#:   反复实机之后下的产品决定：闯关是**扇区推进**的，掉队的那一个会把整队
#:   的进度钉住，「拖进度」比「像不像真人」严重得多。
#:
#: ★★★ 2026-08-30 第五轮实机后收紧到 **1/4 屏**（D103）。用户的原话：
#:   「不能在第一个真人身后 1/4 屏幕以上的后方停下来，如果自己距离第一个
#:   真人身后 1/4 屏幕以上，则必须往前走。」3/4 屏那个数太松 —— 三个 bot
#:   全在 700 多的地方晃，镜头照样被钉住。
BOT_COOP_LEASH = BOT_VIEWPORT_WIDTH / 4.0

#: ★★ 掉队之后要追回到多近才算「归队」—— 比触发线**更紧**的滞回（§141）。
#:
#: 实机（2026-08-30 第四轮）量出来的事实：bot 和带头的真人**极速相同**
#: （都是 `FastRunRate` 1.5 倍走速），差距一旦拉开，双方都在冲刺时它就
#: **冻结**，永远缩不回去 —— bot 全程钉在触发线上、每 130ms 在
#: 「掉队 / 归队」之间翻转一次，恰好停在把镜头钉死的那个位置上。
#: 触发线和释放线分开之后，「掉队了」是一个稳定的区间而不是一条会抖的线。
#: ★ 释放线取触发线的一半（1/8 屏）：追回来时要**往前多追一段**才撒手，
#:   撒手的地方离后界远一点，才不会一松手就又贴回边缘上（D103 的「前面
#:   比后面好」）。
BOT_COOP_LEASH_RELEASE = BOT_COOP_LEASH / 2.0

#: ★★★ **不许超过带头的真人多远**（用户 2026-08-30 第五轮，D103）：
#:
#: 「但是不能超过最前方的真人 1/3 屏幕以上，如果超过第一个真人 1/3 屏幕
#:  以上，则停下来等。等的时候不能影响打怪或躲避的判定。」
#:
#: 于是 bot 的**活动范围**就是 `[带头的人 − 1/4 屏, 带头的人 + 1/3 屏]`。
#: 超前是「停下来等」（走位不再往前），**不是往回走** —— 往回走等于把
#: 刚推进的进度吐回去，而且打怪 / 躲避这两条照旧管用（它们不看这条带）。
BOT_COOP_AHEAD_LIMIT = BOT_VIEWPORT_WIDTH / 3.0


def _quest_forward(terrain):
    """闯关图的「前方」是 +x 还是 −x —— 拿**出生点**问出来的。

    闯关是横版推进：全队从出生点那一头往另一头打。所以「前」= 从出生点
    堆的重心指向地图另一侧的那个方向。★ 这不是「所有图都往右」这种拍脑袋
    的假设，是每张图自己的数据说的（`terrain.points` 就是 `.map` 里那张
    出生点表，真人重生走的也是它）。
    """
    if terrain is None:
        return 1
    spawns = [p for otype in (SPAWN_TYPE_TEAM_A, SPAWN_TYPE_TEAM_B, 108)
              for p in terrain.points.get(otype, ())]
    if not spawns:
        return 1
    mean = sum(float(p[0]) for p in spawns) / len(spawns)
    return 1 if mean <= terrain.width * 0.5 else -1


def _coop_leader(room, forward):
    """闯关时跟谁：**最靠前的那个真人**（用户 2026-08-30）。

    以前跟的是「离自己最近的真人」。房里只有一个真人时两者一样，但一旦
    bot 掉了队，「最近」会把它锚在**后面**那个人身上 —— 几个 bot 互相
    当参照物，整队就一起停在图的左边，真人推不动进度。
    改成恒定盯住**推进得最远**的那一个：他走多远，bot 就得跟多远。
    """
    humans = _followable_humans(room)
    if not humans:
        return None
    return max(humans, key=lambda conn: forward * conn.sync_trail[-1][0])


def _coop_goal(room, machine, seat_index, terrain):
    """闯关时该往哪走：**带头那个真人此刻站的地方**。

    ★ 锚是他**此刻站的地方**，不再是他走过的轨迹（D16 那条老路）：
      轨迹是他绕过的每一个弯，跟着重走既慢又白绕；而中间那段路现在有
      A\\* 自己会走（M5-G），不需要拿轨迹保证「踩得到地面」。

    ## ★★★ 不再有「按名次排的固定点位」（用户 2026-09-01，D114）

    > 我希望不要跟随固定点位，我原话说的是 bot 要在带头真人的后 1/4 屏 ~
    > 前 1/3 屏范围内，只要进入了这个范围内，bot 就没必要再继续调整身位。

    以前这里是 `他 + (前界 − 120 × 名次)` —— 一个**点**。于是 bot 超过了
    自己那个点、又还没到全局前界的那一段会**掉头往回走**，走回它刚跳过去
    的岩浆坑（用户 2026-09-01：「有时候已经跳过去了，它却还要往回走」）。
    现在目标就是带头的人本人，而「到没到」由 `_coop_intent()` 用**活动带**
    判 —— 带内不再调整身位，一个字的排位逻辑都不需要。
    """
    forward = _quest_forward(terrain)
    leader = _coop_leader(room, forward)
    if leader is None:
        return None
    x, y = leader.sync_trail[-1][:2]
    return (float(x), float(y))


def _coop_in_band(lag):
    """在**活动带**里吗 —— `[带头的人 − 1/4 屏, 带头的人 + 1/3 屏]`（D114）。

    `lag` 是 `_coop_lag()` 的第一格：落后带头的人多远，负数 = 已经超前。
    这就是用户 2026-08-30 / 2026-09-01 两次都在说的那同一个范围，
    两条边界是现成的常量，不是新数。
    """
    return -BOT_COOP_AHEAD_LIMIT <= lag <= BOT_COOP_LEASH


def _coop_intent(room, machine, seat_index, terrain, target):
    """闯关模式这一帧往哪走（M5-G）。"""
    if _boss_room(room):
        # ★ boss 房里没有「跟上队伍」这回事 —— 朝 boss 打（§141）。
        return _boss_fight_intent(room, machine, seat_index, terrain, target)
    lag = _coop_lag(room, machine, terrain)
    spot = _coop_goal(room, machine, seat_index, terrain)
    if lag is None or spot is None:
        _clear_navigation(machine)
        return (0, False, False, False)
    # 超过带头真人前界时仍然站住等：框外有怪也不能借机把
    # 任务进度带整个吐到前面去。打怪/躲子弹仍在它之前判。
    if lag[0] < -BOT_COOP_AHEAD_LIMIT:
        _clear_navigation(machine)
        return (0, False, False, False)
    visible_mob = None
    for mx, my, _handle in live_mobs(room):
        if _in_sight(machine, mx, my):
            visible_mob = (float(mx), float(my))
            break
    if target is None and visible_mob is None:
        blind = _blind_intent(room, machine, seat_index, terrain,
                              spawn_fallback=False)
        if blind is not None:
            return blind
    elif target is None and visible_mob is not None:
        # 怪已在框内，只是当前武器被地形挡住/弹道解不出：朝它
        # 挪，不要因为自己正好在任务活动带里就站着发呆。
        return _walk_to(room, machine, terrain, visible_mob, False)
    # ★★★ **在活动带里就不再调整身位**（用户 2026-09-01，D114）。
    #   超前那一头照旧是「停下来等，不往回走」（D103）：往回走等于把刚
    #   推进的进度吐回去，而且往回那一步经常就是走回刚跳过的坑。
    #   ★ 只按**前进轴**判，不看 y —— 以前还要求
    #     `abs(body.y - spot[1]) <= GOAL_Y`，而跟随点的 y 是抄带头真人的：
    #     bot 落在对岸更高的台子上就永远判「没到」，接着 A\\* 去找一条
    #     下到他那一层的路，又是一次往回走。
    #   ★ 这只停**走位**：打怪 / 躲子弹 / 捡道具都排在这个函数前面，照旧。
    if _coop_in_band(lag[0]):
        _clear_navigation(machine)
        return (0, False, False, False)
    if lag[0] < 0.0:
        return (0, False, False, False)     # 超前出带：站住等
    # ★★ **掉队出带了就跑**（用户 2026-08-30：「让 bot 尽量往前走」）。
    #    真人是按着右键冲刺推进的（`FastRunRate` 1.5 倍），bot 只用走速
    #    的话**永远**追不上，一路被落下 500~800 个单位，卡在屏幕左边
    #    把镜头钉住 —— 实机日志里量出来的就是这个数。
    #    ★ 落后到这儿的一般已经由 `_coop_leash_intent()`（触发 1/4 屏、
    #      释放 1/8 屏的滞回）接管了；这条是它释放之后的那一小段。
    return _walk_to(room, machine, terrain, spot,
                    lag[0] > BOT_FOLLOW_DISTANCE and _may_fast_run(machine))


def _may_fast_run(machine):
    """体力够不够按着右键跑一整帧（原版 `FastRunRate` 的开关，不是新规则）。"""
    return (machine.stamina is None
            or machine.stamina > _stamina_props().fast_run_sp_cost
            * botmove.TICKS_PER_BEAT)


def _boss_room(room):
    """这一局是不是在 boss 房里（§141）。

    进 boss 图那一刻控制者会广播 `fileName=data/quest/questNN/
    questNNSxboss.ini` + `type=start`（`note_ai_message` 看到就把
    `quest.boss_room` 置起来，换图跟着清）。boss 房里没有「往前推进」
    这回事 —— 门要等 boss 死才开，把带头的人押在身上没意义。
    """
    quest = None if room is None else room.quest
    return bool(getattr(quest, "boss_room", False))


def _nearest_mob(room, body):
    """离 `body` 最近的那个已知怪的位置；一只都不知道返回 `None`。"""
    mobs = live_mobs(room)
    if not mobs or body is None:
        return None
    row = min(mobs, key=lambda m: math.hypot(m[0] - body.x, m[1] - body.y))
    return (float(row[0]), float(row[1]))


def _boss_fight_intent(room, machine, seat_index, terrain, target):
    """★ boss 房里这一帧往哪走：**朝着 boss 打，不跟人**（§141）。

    用户 2026-08-30 第四轮实机：「bot 进入 boss 房间后，就没必要再有推进
    进度的限制了，因为 boss 房间内只需要打 boss，不需要推进进度。」

    规则还是 `_move_intent` 那两条，只是目标从「带头的人」换成 boss：
    **打得到就站住打，打不到就朝它走过去**。
    boss 的位置来自**命中的采样**（`note_mob_hit`，§141）—— boss 的
    `setState` 从来不带 posX，真人打中它的每一发都在替我们量它在哪。
    还没人打中过它（表是空的）就朝它最近一次开枪的位置走
    （`quest.boss_gun`），连枪都没开过才站住等。

    ★★ 「打得到」的判据就是**这一帧真的有得打**：`target` 是
      `_fire_target()` 的结果（弹道解得出来 / 引信飞得到 / 地形不挡 /
      看得见，全在里面），不是「距离够近 + 屏幕内」—— 隔着掩体站住
      会**永远开不了枪**还站着不动（复审抓的就是这个）。和对战房
      「打得到就站住打」同一条规则、同一个来源。
    """
    body = machine.body
    if target is not None:
        _clear_navigation(machine)
        return (0, False, False, False)
    visible = [row for row in live_mobs(room)
               if _in_sight(machine, row[0], row[1])]
    if not visible:
        # ★ `may_hide=False`：boss 房的门要**打死 boss 才开**，血少了
        #   躲起来等于把这一关钉死（§141 那条「boss 房里没有推进进度
        #   这回事」的另一半）。粗方位照给，只是不许转隐蔽。
        blind = _blind_intent(room, machine, seat_index, terrain,
                              spawn_fallback=False, may_hide=False)
        if blind is not None:
            return blind
    spot = _nearest_mob(room, body)
    if spot is None:
        # 一只都还不知道（真人还没打中过 boss）—— 但它可能**已经开过枪**
        # （`quest.boss_gun`，替它发的 `rpFire` 枪口，§141）。
        # 开不了枪（没有句柄），至少走位该朝它去。
        quest = None if room is None else room.quest
        spot = getattr(quest, "boss_gun", None)
    if spot is None:
        _clear_navigation(machine)
        return (0, False, False, False)
    return _walk_to(room, machine, terrain, spot, False)


# ---------------------------------------------------------------------------
# ★★★ 闯关模式的**牵引绳**（D99）—— 优先级压过一切，包括躲子弹
# ---------------------------------------------------------------------------
def _coop_lag(room, machine, terrain):
    """沿「前方」轴，这个 bot 落在**最靠前那个真人**后面多少。

    返回 `(落后多少, 带头的人, 前方是 +1 还是 −1)`；队伍还不知道在哪、
    或者自己还没有身体时返回 `None`。负数 = 已经冲到他前面去了。
    """
    body = machine.body
    if body is None or terrain is None:
        return None
    forward = _quest_forward(terrain)
    leader = _coop_leader(room, forward)
    if leader is None:
        return None
    return (forward * (float(leader.sync_trail[-1][0]) - body.x),
            leader, forward)


def _coop_leash_intent(room, machine, seat_index, terrain):
    """★★★ 掉队超过 1/4 个屏幕时的**最高优先级**动作；没掉队返回 `None`。

    没掉队时这个函数一分钱都不花（一次减法），行为和以前一模一样 ——
    躲子弹、打怪、捡道具照旧按原来的次序走。

    ## 掉队之后做两件事

    1. **无条件往前追**：目标就是平时那个跟随点，体力够就按着右键冲刺。
       这一帧不躲子弹、不停下来打怪 —— 掉队的代价（挨枪）比拖住整队小。
    2. **实在走不过去就瞬移**（用户 2026-08-30 明确点头）。

    ## 「走不过去」是怎么判的 —— **事件，不是计时器**（铁律 10）

    三条，任一条成立就算：

    * **规划器把话说死了**：`botnav.plan()` 泛洪完整个可达分量之后回了空
      （`nav_failed` 记的就是这个判决，键是当前的空间事实）。这是 A\* 自己
      给的结论「从这儿一步都靠近不了目标」，不是等出来的；
    * **差距比掉队那一刻又拉开了一整根绳子**：`leash_mark` 是掉队以来自己
      走到过的**最靠前**的地方，`leash_gap` 是掉队那一刻的差距 ——
      `他现在的位置 − 我最远走到的地方 > leash_gap + 一根绳子` 就是
      「这段时间他走了一整屏，我一步都没往前挪」。这条管的是「方向键
      按着、人却在墙根原地蹦」那种：规划器不会给出判决，但这是明摆着的
      空间事实。★ 正常在走的时候 `leash_mark` 跟着往前，左边跟着变小，
      这条永远不会成立。
    * ★ **他跑满了一整屏我还是掉着队**（§141，第四轮实机加的）：
      `leash_anchor` 是掉队那一刻带头的人的位置 —— 他又前进了
      `BOT_VIEWPORT_WIDTH` 而我还掉着队，就是「追不上」：
      双方极速相同（都是冲刺），差距在奔跑中是**冻结**的，只会在他停下来
      时才缩得回去。这不是「路走不过去」，是「追不近」—— 用户说的
      「要等一会儿才能跟上」等的就是他停下来那一刻；牵引绳等不起，
      直接瞬移归队。

    第一条尤其要紧：真人**停下来等 bot** 的时候，跟随点不动、bot 也不动，
    两边的空间事实全部冻住 —— 谁都不会再产生新事件。会话 42 之前那个
    「怎么修都还在」的死锁就是它（用户 2026-08-30：「每次都走不动太烦了」）。
    """
    lag = _coop_lag(room, machine, terrain)
    if lag is None:
        _leash_release(machine, seat_index)
        return None
    behind, leader, forward = lag
    # ★ 触发线（1/4 屏）和释放线（1/8 屏）是**两个数**：掉队中的 bot 要追
    #   过头一截才算归队。写成同一个数的话 bot 会钉在线上抖 —— 实机
    #   第四轮三只 bot 全程 749~828、每 130ms 翻转一次「掉队 / 归队」。
    limit = BOT_COOP_LEASH_RELEASE if machine.leash_lagging else BOT_COOP_LEASH
    if behind <= limit:
        _leash_release(machine, seat_index, behind)
        return None
    spot = _coop_goal(room, machine, seat_index, terrain)
    if spot is None:
        _leash_release(machine, seat_index)
        return None
    body = machine.body
    here = forward * body.x
    ahead = forward * float(leader.sync_trail[-1][0])
    if not machine.leash_lagging:
        machine.leash_lagging = True
        machine.leash_mark = here
        machine.leash_gap = behind
        machine.leash_anchor = ahead
        asynclog.emit(f"[{gameserver.ts()}] bot {seat_index}    掉队: 落后带头的人 "
                      f"{behind:.0f}（超过 1/4 屏 {BOT_COOP_LEASH:.0f}）—— "
                      f"无条件冲刺追上去")
    elif machine.leash_anchor is None:
        machine.leash_anchor = ahead    # 瞬移之后重新起算「他跑了多远」
    elif here > machine.leash_mark:
        machine.leash_mark = here          # 还在往前挪，绳子还没绷断
    intent = _walk_to(room, machine, terrain, spot, _may_fast_run(machine))
    stuck_by_plan = (machine.nav_failed is not None
                     and machine.nav_failed == _nav_signature(terrain, body,
                                                              spot))
    # ★ 「一整根绳子」这一条量的是**一整屏**，不跟着触发线走（D103 把触发线
    #   收紧到 1/4 屏之后，拿 256 当尺子会让瞬移变成家常便饭）。
    stuck_by_fact = (ahead - machine.leash_mark) > (machine.leash_gap
                                                   + BOT_VIEWPORT_WIDTH)
    stuck_by_chase = (machine.leash_anchor is not None
                      and ahead - machine.leash_anchor >= BOT_VIEWPORT_WIDTH)
    if stuck_by_plan or stuck_by_fact or stuck_by_chase:
        why = ("A* 说这儿到不了" if stuck_by_plan
               else "按着方向键也没往前挪" if stuck_by_fact
               else "他跑满一整屏我还是追不上（极速相同，差距冻结）")
        _leash_warp(room, machine, seat_index, terrain, spot, behind, why)
        return (0, False, False, False)
    return intent


def _leash_release(machine, seat_index=None, behind=None):
    """没掉队（或者判不了）—— 把牵引绳的记账清掉。

    ★ 日志按**状态翻转**去重：说过一次「掉队了」，就等它真的归了队再说
      一句「归队了」，中间一个字都不刷。
    """
    if not machine.leash_lagging:
        return
    machine.leash_lagging = False
    machine.leash_mark = None
    machine.leash_gap = 0.0
    machine.leash_anchor = None
    if seat_index is not None and behind is not None:
        asynclog.emit(f"[{gameserver.ts()}] bot {seat_index}    归队: 现在落后 "
                      f"{behind:.0f}（归队线 {BOT_COOP_LEASH_RELEASE:.0f}）")


def _leash_warp(room, machine, seat_index, terrain, spot, behind, why):
    """★ 追不上的最后一招：直接把 bot 挪到跟随点上（用户 2026-08-30 点头）。

    落点必须是**站得住的地面** —— 和出生点走**同一条**路
    （`_settle_spawn()`：从那一点往下掉一趟，`on_ground=False` 起步）；
    跟随点悬空就退回带头的人**脚下那一点**（他站得住，那儿一定是合法
    地面）。两个都落不住就不挪，下一帧再说。

    ⚠ 别写成 `botmove.Body(x, y)` —— 那个的 `on_ground` 缺省是 **True**，
      `settle()` 看见就直接原样返回，人会停在半空里。
    """
    lag = _coop_lag(room, machine, terrain)
    candidates = [spot]
    if lag is not None:
        leader = lag[1]
        candidates.append(tuple(leader.sync_trail[-1][:2]))
    for point in candidates:
        body = _settle_spawn(terrain, machine, point)
        if body is None or not body.on_ground:
            continue
        was = machine.body
        machine.body = body
        _clear_navigation(machine)
        machine.leash_mark = _quest_forward(terrain) * body.x
        # 「他跑了多远」从落点重新起算：瞬移完还在掉队的话，得再给他
        # 一整屏的余量才谈得上「又追不上」（§141）。
        machine.leash_anchor = None
        asynclog.emit(f"[{gameserver.ts()}] bot {seat_index}    ★瞬移归队: "
                      f"({was.x:.0f}, {was.y:.0f}) -> ({body.x:.0f}, {body.y:.0f})"
                      f"　落后 {behind:.0f}，{why}")
        return True
    return False


def _retreat_done(body, goal, enemy):
    """这个后撤落脚点是不是该换一个了。

    两种情形：**已经退到了**，或者**敌人绕到了另一边**（原来的落点现在
    是朝着他走）。判据都是位置事实，没有计时器。
    """
    if (abs(body.x - goal[0]) <= botnav.GOAL_X
            and abs(body.y - goal[1]) <= botnav.GOAL_Y):
        return True
    return (goal[0] - body.x) * (enemy[0] - body.x) > 0.0


def _decide(room, machine, seat_index, terrain, now, tick):
    """AI 的那一半：换枪 + 挑目标 + 走位意图。**约 15 Hz，不是每格**。

    ## 为什么和物理分开（D106）

    物理一格 32 ms —— 那是**收方的逻辑步长**，弹体的 `rpExplode` 必须踩着它
    发（§147）。可寻路 / 解弹道 / 评估威胁这几件事既贵又不需要那么勤：
    真人也不会 32 ms 换一次主意。节奏就是原来那个 `BOT_DECISIONS_PER_SECOND`
    （15 Hz ≈ 每 2 格一次），和 D106 之前一模一样，所以行为不变。

    ★ 最贵的 A\\* 早就不在这条路上了（`botplan.PLANNER` 那条后台线程，§137）。

    产出三样，挂在 `machine` 上给后面每一格用：

    * `machine.intent` —— `(方向, 起跳, 下落, 冲刺跑)`，`_own_step()` 逐格消费；
    * `machine.dodge_crouch` —— 蹲不蹲（`_dodge_intent()` 写的）；
    * `machine.aim` —— 挑中的目标，喂心跳里的准星和「该不该扣扳机」。

    ⚠ **真扣扳机的那一格会重解一次弹道**（见 `_tick_bot`）：`rpFire` 里的枪口
    坐标必须和刚走完这一格的位置一致（§62），而这一份最多是 2 格之前算的。
    """
    machine.intent_tick = tick
    # ★★ **换枪排在最前面**（M5-C）：这一格用哪把枪决定了「打不打得到」，
    #    而「打不打得到」又决定了走不走。房主锁了枪 / 手上是捡来的枪时
    #    这一步是空转。
    _choose_weapon(room, machine, seat_index)
    weapon = machine.weapon
    # ★ 先算一次「站在**现在**这个位置打不打得到」：走不走就看它
    #   （`_move_intent` 的第 1 条）。这一发**不带失误** —— 它只回答
    #   「有没有得打」，掷骰子留给下面那一份真正的瞄准。
    standing = (None if weapon is None
                else _fire_target(room, machine, seat_index, weapon))
    # ★★ 提前量 + 失误（M5-D）：`miss` 是**这一发**的偏差，开火前掷一次、
    #    打出去之后清（`_try_fire()` 末尾），所以准星不会逐格抖。
    machine.aim = (None if weapon is None
                   else _fire_target(room, machine, seat_index, weapon,
                                     miss=_aim_miss(room, machine, seat_index)))
    machine.intent = _move_intent(room, machine, seat_index, terrain,
                                  standing, now)
    return machine.intent


def _own_step(room, machine, seat_index, terrain, now, tick):
    """自己走**一格**（32 ms），返回和 `trail_point()` 同格式的那个八元组；
    还接管不了就返回 `None`（调用方退回回放真人轨迹）。

    接管不了的情形只剩一种，退回 D16 那条老路：**没有地形数据**
    （这张图没提取到）—— 没有地面就没法自己走。

    ★★ **闯关房从 M5-G 起也自己走**：以前那儿是纯轨迹回放（D16），
    好处是节奏天然跟真人一致，代价是**一步都躲不开**（回放的是别人的路）。
    现在改成「把跟随点当寻路目标」——节奏还是跟真人的轨迹走（目标就在他
    刚踩过的点上），但中间那一段是自己走的，所以躲子弹 / 跳坑 / 打怪
    全都接得上。队伍还不知道在哪时仍然退回老路。

    ## ★★★ 「场上没有敌人」**不再**退回真人轨迹（§91）

    这里原来的第一句是 `if not _hostile_targets(): return None`。
    真人一死，`_lying_dead()` 把他从敌人表里剔掉 ⇒ 敌人表空 ⇒ bot 当场
    退回 `trail_point(真人轨迹)`，**一瞬间被拽到真人身边**，然后每一帧
    在「自己算的点」和「真人轨迹上的点」之间来回跳 —— 用户 2026-08-28 报的
    「我被打死的一瞬间 bot 瞬移到我身边然后不停抽搐」就是这一句。

    没有敌人是**很正常的一刻**（对面全躺着等重生），这时候 bot 该做的是
    站在原地 —— `_move_intent()` 本来就会返回全 false 的动作。

    ## ★★★ 一格就是一格（D106）

    以前这里按「距上一次多久」算该补几个 tick，还带一个余数累加器
    （那个 `move_at` 累加器）—— 那是因为帧率跟着真人心跳走、忽快忽慢。
    现在房间循环
    就是 32 ms 一格，这个函数**恒推一格**，累加器整个不需要了。
    §120 那条「逐 tick 重新决策」也自动成立：起跳 / 按 ↓ 这类一次性动作
    本来就只在某一格上生效，而现在每一格都是单独的一次调用。
    """
    machine.move_down = False
    if terrain is None:
        return None
    who = _character_of(machine)
    if machine.body is None:
        # ★★★ 第一格的锚是**这个座位该用的地图出生点**（§91）——
        #   和真人走同一套分配规则，所以客户端自己算出来的位置和这边一致，
        #   第一发心跳不会把模型拽走。
        anchor = _spawn_point(room, seat_index, terrain)
        if anchor is None:
            # 这张图没有出生点对象（产物里 `points` 是空的）—— 退回 D16：
            # 真人站过的地方一定是合法地面。
            if machine.battle_pos is None:
                return None
            anchor = machine.battle_pos
        machine.body = _settle_spawn(terrain, machine, anchor)
    # ★★ 进图那两秒**连走都不许走**（§94）—— 屏幕上还在放「预备 / 开始」，
    #    真人这时候也动不了。心跳照发（站着的姿势），只是不迈腿；被顶飞的话
    #    下面照样把它推出去（`direction` 在空中本来就不起作用，§93）。
    #    ★ 这一条**逐格**问：它是「到点没到点」的事实，不是 15 Hz 的决策。
    if _may_walk(machine, now):
        direction, want_jump, want_drop, fast_run = (
            machine.intent or (0, False, False, False))
    else:
        direction, want_jump, want_drop, fast_run = 0, False, False, False
    machine.move_down = bool(want_drop)
    crouched = bool(machine.dodge_crouch)
    speed_scale = _speed_scale(machine, now)
    before = machine.body
    # ★★★ **第二段跳在物理这一层按**（V0.3 §151），和 ↓ 的锁存同一个道理。
    #   它不是「这一格想干什么」，是**这一段飞行**起跳时就欠下的一个动作：
    #   谁规划了这一跳（A\* 的 `Step.double` / 兜底那条），谁就把旗子挂上，
    #   到了顶点由这里按下去。
    #   ★ 放在决策层的两个后果，实机都吃过：
    #     ① 顶点落在**非决策格**上时晚一格，和 `double_jump_lands()`
    #        逐格模拟出来的弧线对不上；
    #     ② 飞到一半命中 `_move_intent()` 的任何一条早退分支（躲子弹 /
    #        打得到就站住 / 闯关那几条）就**再也没人按了** ——
    #        一段跳掉进岩浆。旗子保住了也没用，得有人真按下去。
    if (not before.on_ground and machine.nav_double_jump
            and botmove.at_apex(before)):
        want_jump = True
    machine.body = botmove.tick(terrain, before, who,
                                direction=direction, fast_run=fast_run,
                                crouched=crouched,
                                want_jump=want_jump, want_drop=want_drop,
                                speed_scale=speed_scale)
    left_ground = before.on_ground and not machine.body.on_ground
    if machine.nav_path and left_ground:
        machine.nav_started = True
    jumped = 0
    if want_jump:
        if left_ground:
            jumped = 1
        elif machine.body.air_jumped and not before.air_jumped:
            # ★ 第二段跳（§124）—— `rpJump` 的段号要报 2，不是 1。
            jumped = 2
        # ★★ 跳的意图**用掉就作废**：不清的话下一格还举着 `want_jump=True`，
        #    而腾空中按跳 = 第二段跳（§124），白白多跳一段。
        if not machine.body.on_ground and machine.intent is not None:
            machine.intent = (direction, False, want_drop, fast_run)
    body = machine.body
    return (body.x, body.y, jumped, body.on_ground, body.vx, body.vy,
            bool(fast_run), crouched)


def _path_blocked(terrain, x0, y0, shot, radius=0.0):
    """这条弹道中途会不会撞上地形。

    ★ 用 `blocks_bullet()` 那一路（`mapdata.line_blocked`），**不是**
    `is_solid()`：单向平台（那种细白线）挡人**不挡子弹**（§29）。

    ★ 抛物线要**分段**查：`ballistics.path_points()` 把弧切成几段，每段当
    直线看的误差比地形位图一个像素还小（那边的注释算了）。直射弹只有一段。

    ★ 没有地形数据时（这张图没提取到 / 产物缺失）返回 `False` = 「不知道
    有没有挡」，宁可 bot 偶尔打一发穿墙的，也不要让它一枪不放。

    ## ★★★ 第一件事是查**枪口那一点自己**（§76）

    `line_blocked()` 的两个端点都不采样（那边的注释写了为什么：枪口和目标
    常常贴着地面）。可 bot 站在斜坡脚下往上打时，「脚 + 前 43 + 上 57」
    这个枪口会**整个埋进山体里** —— 这时收方的弹体一出生就撞在地形上、
    速度每帧对折反向、**位置一步不动**（客户端逐帧日志实测），
    玩家**根本看不见这一发**；而服务端按闭式解一路飞下去，
    十几个 tick 之后在别处炸开，还带着半径 100 的溅射。

    用户 2026-08-28 报的「看不到扔出去的手雷，过一会儿在旁边出现了爆炸
    动画和火焰」和「明明躲开了还是打到我身上」，是同一件事的两个面。

    ⇒ 枪口埋在挡子弹的地形里 = **这一发打不出去**，和「中间被墙挡住」
    是同一类事实，不是新规则。

    ★ `radius` = 弹体的 `Size`：枪口那一格要和 `_terrain_contact()` 一个
    口径（§83），不然会出现「开火时觉得通、结算时当场撞在枪口上」——
    那一发在收方看不见，服务端却在自己脚下炸一个半径 100 的溅射。
    **中途那几段还是按圆心查**（`line_blocked`）：那边只决定「值不值得
    开这一枪」，粗一点只影响 bot 的积极性，不影响爆炸点。
    """
    if terrain is None:
        return False
    # ★ 采样点组和 `_terrain_contact()` 必须是同一套（§116）：这里判「枪口
    #   通不通」，那边判「飞到哪儿撞上」，两边口径不一样就会出现
    #   「开火时觉得通、结算时当场撞在枪口上」。
    if radius and radius >= 1.0:
        offsets = shell_probe_offsets(radius, math.cos(shot.angle),
                                      math.sin(shot.angle))
    else:
        offsets = ((0, 0),)
    if _probe_blocked(terrain, int(x0), int(y0), offsets):
        return True
    points = ballistics.path_points(x0, y0, shot)
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        if terrain.line_blocked(ax, ay, bx, by, step=BOT_LINE_STEP):
            return True
    return False


def _shot_impact(terrain, x0, y0, shot, radius=0.0):
    """这条弹道首次撞地形的坐标；飞完都没撞上返回 `None`。

    分段和碰撞形状完全复用 `_terrain_contact()` / `ballistics.path_points()`，
    所以“AI 认为能打在罐子上”和子弹实际结算不会有第二套近似。
    """
    if terrain is None:
        return None
    points = ballistics.path_points(x0, y0, shot)
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        hit_t, _safe_t = _terrain_contact(terrain, ax, ay, bx, by,
                                          radius=radius)
        if hit_t is not None:
            return (ax + (bx - ax) * hit_t, ay + (by - ay) * hit_t)
    return None


#: 解抛物线弹道时，在「刚好够得着」的初速上留多少余量。
#:
#: ★ 它**不是**观测阈值，是**数值余量**：`_lob_speed()` 用连续模型的
#: 最小抛射初速 `sqrt(g(h + √(dx²+h²)))` 当起点，而收方跑的是离散递推
#: （`v.y += a; pos += v`，比连续模型多掉 `a·n/2`），两者差一点点。
#: 不留余量的话 `ballistics.solve()` 的判别式会卡在 0 附近解不出来。
BOT_LOB_MARGIN = 1.06

#: 找「够得着的最小初速」时最多往上试几档（每档 × `BOT_LOB_MARGIN`）。
#: 到顶还解不出来就是这把枪真够不着 —— 循环上界，防死循环，不是时序阈值。
BOT_LOB_STEPS = 24


def _lob_speed(weapon, dx, dy):
    """抛物线武器该用多大力气扔：**刚好够得着**的那一档（§66）。

    ## 为什么不是「用最大力」

    `ballistics.max_speed()` 给的是这把枪的**上限**，而 `solve()` 取的是
    低抛解 —— 两个一叠加，手雷就成了「贴着地平线飞过去的直球」。
    用户 2026-08-27 实机报的「手榴弹扔出去的速度太快了，真人对战时
    手榴弹飞得很慢」说的就是它。

    真人扔手雷是**蓄力**的（`PowerControl=1 / 2`，`rpFire +18` 那一格在
    语料里 8~531 全谱都有），朝着几百个单位外的人不会拉满 —— 拉满是
    留给极远距离的。「刚好够得着」这个口径给出的正是课本上那条
    **最小能量抛射**：仰角 ~45°、飞得慢、弧线看得清，而且射程一到就自然
    地拉满力气。

    ## 怎么算

    连续模型的最小初速是 `v² = g(h + √(dx² + h²))`（h = 抬升高度，
    这里 `dy` 往下为正所以 `h = −dy`）。拿它当起点，逐档 × 余量往上试，
    第一个 `solve()` 解得出来的就是答案；到 `max_speed()` 还解不出来
    就返回 `None`（这把枪真的够不着）。

    直射武器（`GravityFactor = 0`）不走这条路 —— 它们没有蓄力，
    `power` 恒 1.0，速度就是 `Velocity`。

    ## ★★ 蓄力武器只能取**离散**的档（§73）

    `PowerControl=2` 的蓄力计数器每 tick `+2`、封顶 80，松手时夹进
    `[15, 80]` ⇒ `power` 只可能是 `{15} ∪ {16, 18, …, 80}`
    （语料 3036 发一个例外都没有）。所以算出来的连续速度要**往上**
    snap 到最近的合法档 —— 蓄不够就够不着，而且蓄多久也是按这个数算的。
    """
    ceiling = ballistics.max_speed(weapon)
    gravity = ballistics.gravity_per_tick(weapon)
    if not gravity or ceiling <= 0:
        return ceiling
    lift = -float(dy)
    speed = math.sqrt(max(1e-6, gravity * (lift + math.hypot(dx, lift))))
    speed *= BOT_LOB_MARGIN
    for _ in range(BOT_LOB_STEPS):
        if speed >= ceiling:
            return ceiling
        if ballistics.solve(weapon, dx, dy, speed=speed) is not None:
            return _snap_charge(weapon, speed, ceiling)
        speed *= BOT_LOB_MARGIN
    return ceiling


def _snap_charge(weapon, speed, ceiling):
    """把连续初速抬到蓄力武器**够得着的那一档**（§73）；别的武器原样返回。"""
    if weapon.power_control != ballistics.MODE_CHARGE:
        return speed
    snapped = ballistics.speed_for_power(
        weapon, ballistics.charge_power(weapon, speed))
    return min(ceiling, max(speed, snapped))


def _aim_point(room, seat_index, x, y, crouched):
    """瞄这个人身上的哪一点 —— **身体那个圆的圆心**（`chrprops`）。

    ★ 会话 18 把瞄准点抬到 `BOT_MUZZLE_HEIGHT`（脚上 57）是「枪口对枪口」，
    那时候命中是服务端硬判的、瞄哪儿都打得中，所以怎么瞄都行。
    现在命中是**真判**的（§65），瞄准点必须落在碰撞圆里 —— 而三个圆里
    身体那个最大，也最不容易因为两人站位高低差几个单位就擦过去。
    """
    seat = room.seats[seat_index] if 0 <= seat_index < len(room.seats) else None
    character = chrprops.get(0 if seat is None else seat.character_id)
    return character.center(x, y, crouched)


#: ★★ **进图 / 复活之后多久才能动手**（秒）。
#:
#: 原版 `Character::Respawn`（`0x502fca`）里：
#:
#:     0x5030a0  mov eax, 0x7d0            ; 2000 ms
#:     0x5030a6  idiv [0x6dc528]           ; ÷ 32 ms = 62 个 tick
#:     0x5030b9  call 0x401b0f             ; 给 [char+0x6a0] 挂上状态 0
#:
#: 而**开火输入**（`0x516471: push 0 … call 0x401c0c; jne 跳过`）和
#: **近身输入**（`0x515acc`）进门第一件事就是查状态 0 在不在。
#: ⇒ 真人在这 2 秒里只能跑、不能打。
#:
#: ★ 语料独立对上了：953 局里「房主发 `0x0402` 到本局第一发 `rpFire`」
#: 最少隔 **15 发心跳**（15 × 128 ms = 1.92 秒），p05 = 17 —— 一条很硬的地板。
#:
#: ★ 这**不是**我们定的时序阈值（铁律 10）：它是原版写死的常量，
#: 服务端只是照抄；起算点是**事实翻转**（进了 IN_GAME / 从躺着站起来），
#: 不是定时器。
BOT_ACTION_LOCK_S = 2.0


def _note_action_lock(room, machine, seat_index, now):
    """维护「现在能不能动手」这道锁 —— 按**状态翻转**上锁（§74）。

    两个上锁点，都对应原版调 `Character::Respawn` 的那两处：

    1. **这一局的第一帧**（`act_lock_until is None`）—— 客户端刚被
       `0x0402` 推进 stage 7，角色放进图里，屏幕上正在放「预备 / 开始」；
    2. **躺着 -> 站起来**（`_lying_dead()` 翻转）—— 重生广播刚发出去。
    """
    lying = _lying_dead(room, seat_index)
    if machine.act_lock_until is None:
        # ★★ 进图那一档：屏幕上正在放「预备 / 开始」，**连走都还不能走**
        #   （§94）。所以这一档比复活那一档多挂一个 `enter_lock_until`。
        machine.act_lock_until = now + BOT_ACTION_LOCK_S
        machine.enter_lock_until = machine.act_lock_until
    elif machine.was_lying and not lying:
        # 复活那一档：真人这两秒**只能跑不能打**（用户 2026-08-27 的原话），
        # 所以只挂动手那道锁，走位照旧。
        machine.act_lock_until = now + BOT_ACTION_LOCK_S
    machine.was_lying = lying


def _may_act(machine, now):
    """这一帧允许开枪 / 近身吗（`BOT_ACTION_LOCK_S` 那道锁）。"""
    until = machine.act_lock_until
    return until is None or now >= until


def _may_walk(machine, now):
    """这一帧允许**走位**吗 —— 只有**进图**那一档拦（§94）。

    ★ 为什么单独一道：M5 之前 bot 的位置是**回放真人轨迹**的（D16），
    真人不动它就不动，所以「预备 / 开始」那两秒天然不会抢跑；M5 让它自己
    走之后，第一发心跳一到它就出发了 —— 用户 2026-08-28 报的
    「一开始我还不能动呢，就看见 bot 已经向我这边跑来了」。

    ⚠ 复活那一档**不拦**：用户 2026-08-27 说得很清楚
    「被打死复活后有大概两三秒不能开枪，**只能移动**」。
    """
    until = machine.enter_lock_until
    return until is None or now >= until


def _charge_ready(machine, weapon, shot, now):
    """蓄力武器：手指按够了吗（§73）。

    非蓄力武器恒为 `True` —— 它们**点击即发**（`0x5164c8` 那个
    `cmp [weapondef+0x18], 2` 的另一支）。

    蓄力武器要按住 `ballistics.charge_ticks(power)` 个逻辑 tick。
    这里只回答「按够了没有」，按下去那一刻由 `_hold_trigger()` 记。
    """
    if weapon.power_control != ballistics.MODE_CHARGE:
        return True
    if machine.charge_at is None:
        return False
    need = (ballistics.charge_ticks(shot.power)
            * ballistics.TICK_MS / 1000.0)
    return (now - machine.charge_at) >= need


def _hold_trigger(machine, weapon, target, now):
    """按住 / 松开鼠标左键的记账（蓄力武器专用，§73）。

    * 有目标 + 是蓄力武器 -> 手指按着（第一次按下时记时刻）；
    * 没目标 / 换成了非蓄力武器 -> 手松开，蓄力清零。

    ⚠ **目标换人不清零**：真人是先按住蓄着、再挑往哪扔的，
    中途改主意不会把力气泄掉。
    """
    if target is not None and weapon is not None \
            and weapon.power_control == ballistics.MODE_CHARGE:
        if machine.charge_at is None:
            machine.charge_at = now
    else:
        machine.charge_at = None


def _charge_value(machine, now):
    """此刻手指按出来的**蓄力值**，填进心跳 `+15`（= `[char+0x594]`）。

    收方那一格就是蓄力计数器（packet_api §5.5，语料 59117/67186 是 0 ——
    因为绝大多数时候没人在蓄力）。报了它，别人屏幕上才看得见 bot 在
    「攒力气」。没按着返回 0。

    数值按收方的算法走（§73）：第 k 个 tick 上是 `max(4, 2k)`，封顶 80。
    """
    if machine.charge_at is None:
        return 0
    ticks = int(max(0.0, now - machine.charge_at)
                * ballistics.TICKS_PER_SECOND)
    if ticks <= 0:
        return 0
    return min(ballistics.POWER2_MAX,
               max(ballistics.POWER2_FLOOR,
                   ticks * ballistics.POWER2_CHARGE_STEP))


def _outlives_fuse(weapon, shot):
    """这一发在飞到目标之前会不会先被**引信**炸掉（§72）。

    带引信的弹体（`AppleGrenade` / `SeedBomb` / `SliceBullet`）在第
    `fuse_ticks` 个 tick 上自爆 —— 那一下**不带伤害**（伤害只来自射手发的
    `rpExplode`），而且弹体从此不存在，之后再发 `rpExplode` 会被收方
    按句柄查不到而整包丢掉（§42 第 4 条）。

    ★ 判据是**原版数据**（`SliceTime`），不是我们定的超时（铁律 10）。
    没有引信的武器恒为 `False`。
    """
    fuse = weapon.fuse_ticks
    if not fuse:
        return False
    # ★ 收方能飞完 `fuse-1` 个 tick，第 `fuse` 个 tick 上就炸了 ——
    #   所以「飞到目标」必须严格发生在第 `fuse` 个 tick 之前。
    return shot.ticks >= fuse - 1


def _fire_target(room, machine, seat_index, weapon, miss=None):
    """挑一个能打的目标，返回 `(座位号, 瞄准点(x, y), Shot)`；没得打返回 `None`。

    这是「射手那台机器」原本要做的判断，现在归服务端（D28）：

    1. **够得着** —— 直线距离在 `BOT_ENGAGE_RANGE` 之内（语料量的，见那条常量）；
    2. **弹道解得出来** —— 抛物线武器超出最大射程时 `ballistics.solve()`
       返回 `None`（判别式 < 0），那就是这把枪真的够不着；
    3. ★ **飞得到引信之前** —— 带引信的弹体（`AppleGrenade` / `SeedBomb` /
       `SliceBullet`）在 `fuse_ticks` 那一 tick 上**在每一台机器上自爆**，
       弹体从此不存在（§72）。飞到目标要的 tick 数够不着引信，就是这把枪
       在这个距离上**打不到**，和「弹道解不出来」是同一类事实；
    4. **看得见** —— 弹道上没有挡子弹的地形（`_path_blocked`）；
    5. 都满足的挑**最近**的。

    ⚠ 挑中了**不等于打得中** —— 命中由 `_advance_shells()` 逐 tick 真判
    （§65）。这里只回答「值不值得扣扳机」。

    ★★ **这里没有「不往自己爆炸半径里开炮」这一条**（D50，别再加回来）。
    溅射确实会炸到自己（§69），但真人对局里贴脸对射把自己一起炸死是**常态**
    —— 真人是自己权衡「炸掉他值不值得挨这一下」，而不是守着一条「近了就
    不许开」的禁令。这种权衡要等真 AI 来做，现在**不替它拍板**。
    """
    option = _engagement(room, machine, seat_index, weapon, miss=miss)
    if option is None:
        return None
    shot = option.shot
    if machine.slow_bullet:
        shot = _slow_shot(weapon, shot)
    return (option.seat, option.point, shot)


class Engagement(collections.namedtuple(
        "Engagement", "seat point shot span speed hit_radius")):
    """一次「这把枪现在打得到谁」的完整答案 —— 开火和武器评分共用它。

    `span` 是枪口到目标的距离，`speed` 是目标的速度（单位 / tick），
    `hit_radius` 是命中窗口（目标身体那个圆 + 弹体半径）。后两项只有
    `botarms.score()` 用得上，但它们是解这一发时顺手就有的事实。
    """

    __slots__ = ()


def _solver_for(weapon):
    """把武器和「用多大力气」绑进一个 `solve(dx, dy) -> Shot|None`。"""
    def solve(dx, dy):
        return ballistics.solve(weapon, dx, dy,
                                speed=_lob_speed(weapon, dx, dy))
    return solve


BREAKABLE_SEAT = -2


def _path_breakable_item(room, machine, terrain=None):
    """当前锁定的挡路物；已碎/索引失效就就地清掉。"""
    index = getattr(machine, "path_breakable", None)
    if index is None:
        return None
    terrain = _terrain(room) if terrain is None else terrain
    items = () if terrain is None else getattr(terrain, "breakables", ())
    alive = frozenset() if terrain is None else getattr(terrain, "alive",
                                                        frozenset())
    if not 0 <= int(index) < len(items) or int(index) not in alive:
        machine.path_breakable = None
        machine.path_breakable_prefix = []
        machine.path_breakable_only = False
        return None
    return items[int(index)]


def _breakable_aim_points(item, x, y):
    """从射手这一侧尝试的几个点：先正对着的表面，再中心/四边。"""
    near_x = min(max(float(x), item.left), item.left + item.width - 1)
    near_y = min(max(float(y), item.top), item.top + item.height - 1)
    face_x = item.left if x <= item.x else item.left + item.width - 1
    face_y = item.top if y <= item.y else item.top + item.height - 1
    raw = ((float(face_x), near_y), (near_x, float(face_y)),
           (float(item.x), float(item.y)),
           (float(item.left), float(item.y)),
           (float(item.left + item.width - 1), float(item.y)),
           (float(item.x), float(item.top)),
           (float(item.x), float(item.top + item.height - 1)))
    out = []
    for point in raw:
        if point not in out:
            out.append(point)
    return tuple(out)


def _breakable_option(room, machine, weapon, terrain, solve, x, y):
    """这把枪能不能真的打到当前挡路物。

    返回 `(Engagement, 这一发实际伤害, 爆炸点)`。中途撞到别的
    地形/另一件物体、引信先炸、或 11 个原版采样点一个都没碰到
    目标时都返回 `None`。
    """
    item = _path_breakable_item(room, machine, terrain)
    if item is None or not _in_sight(machine, item.x, item.y):
        return None
    best = None
    splash_range = float(getattr(weapon, "splash_range", 0.0) or 0.0)
    splash_damage = float(getattr(weapon, "splash_damage", 0.0) or 0.0)
    if splash_damage <= 0.0:
        return None
    for tx, ty in _breakable_aim_points(item, x, y):
        mx, my = _muzzle(x, y, tx)
        span = math.hypot(tx - mx, ty - my)
        if span > BOT_ENGAGE_RANGE:
            continue
        shot = solve(tx - mx, ty - my)
        if shot is None or _outlives_fuse(weapon, shot):
            continue
        impact = _shot_impact(terrain, mx, my, shot,
                              float(getattr(weapon, "size", 0.0) or 0.0))
        if impact is None:
            continue
        preview = botbreak.preview_damage(
            item, impact[0], impact[1], splash_range, splash_damage,
            mult=_damage_scale(room))
        if preview is None:
            continue
        hurt, _where = preview
        engagement = Engagement(BREAKABLE_SEAT, (tx, ty), shot, span, 0.0,
                                item.radius + float(weapon.size or 0.0))
        option = (engagement, hurt, impact)
        if best is None or (hurt, -span) > (best[1], -best[0].span):
            best = option
    return best


def _engagement(room, machine, seat_index, weapon, miss=None):
    """挑最近的那个打得到的敌人，连提前量一起解好；没得打返回 `None`。

    ★★ **提前量**（M5-D）：`botaim.aim()` 把「弹飞到那儿要几个 tick」和
    「他这段时间会走到哪」联立起来迭代。目标站着不动时它退化成原来那套
    「瞄他现在站的地方」，所以这一步对静止目标没有任何行为改变。

    ★★ **失误**（M5-D）：`miss` 是**开火前就掷好**的一份偏差（`botaim.Miss`）。
    给了它就把这一发弄歪，并且**用弄歪之后的点重解弹道** —— 否则包里的角度
    还是准的，屏幕上看不出打偏。
    """
    if machine.battle_pos is None:
        return None
    x, y = machine.battle_pos
    terrain = _terrain(room)
    solve = _solver_for(weapon)
    best = None
    # ★★ **只打看得见的**（§127）：屏幕外的人真人根本不知道在哪，
    #    服务端也不该把精确坐标喂给 bot。看不见时走位归 `_rough_bearing()`。
    for index, px, py, crouched in _visible_targets(room, machine, seat_index):
        tx, ty = _aim_point(room, index, px, py, crouched)
        if BOT_DIAG_FIRE_ANYWHERE:
            # ★ 取证专用：真人站着不动时 `trail_point()` 会让 bot 贴到人身上
            #   （§52），距离 0 的话 `ballistics.solve()` 解不出弹道、一颗
            #   弹体都造不出来。这里改成往旁边**空放**一发 —— 要的只是
            #   「弹体在收方长什么样」，打不打得中无所谓。
            tx, ty = x + 400.0, y - BOT_MUZZLE_HEIGHT - 50.0
        mx, my = _muzzle(x, y, tx)
        span = math.hypot(tx - mx, ty - my)
        if span > BOT_ENGAGE_RANGE and not BOT_DIAG_FIRE_ANYWHERE:
            continue
        if best is not None and span >= best.span:
            continue                       # 已经有更近的了，弹道就别解了
        velocity = ((0.0, 0.0) if BOT_DIAG_FIRE_ANYWHERE
                    else _seat_velocity(room, index))
        radius = _hit_radius(room, index, weapon)
        point, shot = botaim.aim(solve, (mx, my), (tx, ty), velocity,
                                 radius, miss)
        if shot is None:
            continue
        # ★ 提前量可能把瞄准点推到身体另一侧 —— 枪口跟着朝向翻（`_muzzle`），
        #   所以翻了就得拿新枪口重解一次，否则 `_try_fire()` 组包时用的枪口
        #   和这里解弹道用的不是同一个点。
        nx, ny = _muzzle(x, y, point[0])
        if (nx, ny) != (mx, my):
            shot = solve(point[0] - nx, point[1] - ny)
            if shot is None:
                continue
            mx, my = nx, ny
        if _outlives_fuse(weapon, shot):
            continue
        if (not BOT_DIAG_FIRE_ANYWHERE
                and _path_blocked(terrain, mx, my, shot, weapon.size)):
            continue
        best = Engagement(index, point, shot, span,
                          math.hypot(velocity[0], velocity[1]), radius)
    if best is None and not BOT_DIAG_FIRE_ANYWHERE:
        # ★★★ 双路线规划认定的**捷径第一道门**（用户 2026-09-01）。
        #   排在真人后面：`_breaking_now()` 已经把「视野里有活敌人/活怪」
        #   挡掉了，这里再让 `best` 优先一次，是为了和走位那一侧
        #   （`_breakable_move_intent`）用**同一个**门 —— 两边判得不一样
        #   就会出现「走位站着等打罐子、开火却去打别的」的僵局。
        #   ★ 也排在烟雾 / 怪目击点之前：那两条打的是「猜的位置」，
        #     而挡路物的坐标是地图数据，实打实挡着路。
        #   打碎之后 `_refresh_breakables()` 换地形、清目标并重规划。
        if _breaking_now(room, machine, seat_index, terrain) is not None:
            blocked = _breakable_option(room, machine, weapon, terrain,
                                        solve, x, y)
            if blocked is not None:
                return blocked[0]
    if best is None and not BOT_DIAG_FIRE_ANYWHERE:
        # ★★ 一个都挑不中，但有人躲在烟里 ⇒ **朝云团乱射**（M5-F）。
        best = _smoke_engagement(room, machine, seat_index, weapon, terrain,
                                 solve, x, y)
    if best is None and not BOT_DIAG_FIRE_ANYWHERE:
        # ★★ 闯关房：朝**刚看见过的那只怪**打（M5-G / §125）。
        best = _mob_engagement(room, machine, weapon, terrain, solve, x, y)
    return best


#: 「这是一只怪，不是座位」——`_fire_target()` 那个三元组的第一格。
#: 负数保证不会被当成座位号去索引 `room.seats`（那几处都有 `0 <= i` 的门）。
MOB_SEAT = -1


def _mob_engagement(room, machine, weapon, terrain, solve, x, y):
    """朝**最近的一个还新鲜的怪目击点**开枪；没有就返回 `None`（M5-G）。

    ## 服务端凭什么知道怪在哪

    凭控制者广播的 `rpAiMsg`（§125）：`msgType=setState` 里带 `posX` / `posY`，
    `state=death` 报死亡。和别人的客户端拿到的是**同一份**信息 ——
    真人之间能合作看见怪，靠的就是这一发。

    ⚠ 位置是**状态变化那一刻**报的，两次之间怪会自己走（语料：中位 10 个
    单位、p90 81）。所以偶尔打偏是正常的，和真人看着别人屏幕上的怪打差不多。
    """
    best = None
    for mx_, my_, handle in live_mobs(room):
        # ★ 怪也一样只打**看得见的**（§127）：真人的屏幕外也没有怪。
        if not _in_sight(machine, mx_, my_):
            continue
        gx, gy = _muzzle(x, y, mx_)
        span = math.hypot(mx_ - gx, my_ - gy)
        if span > BOT_ENGAGE_RANGE:
            continue
        if best is not None and span >= best.span:
            continue
        shot = solve(mx_ - gx, my_ - gy)
        if shot is None or _outlives_fuse(weapon, shot):
            continue
        if _path_blocked(terrain, gx, gy, shot, weapon.size):
            continue
        best = Engagement(MOB_SEAT, (mx_, my_), shot, span, 0.0,
                          MOB_HIT_RADIUS + float(weapon.size or 0.0))
    return best


def _smoke_engagement(room, machine, seat_index, weapon, terrain, solve, x, y):
    """朝烟雾团里一个随机点开枪的那一发；没有可打的烟返回 `None`。

    ★ 云里的人挑不中是 D67 定的（我们加的规则，原版的烟纯视觉）；
      这里是它的另一半：**挑不中不等于不还手**（用户 2026-08-29）。
      落点每一发换一个（`machine.smoke_offset` 在 `_reroll_aim_miss()` 里清），
      所以看着就是「朝那团烟一通乱放」。
    """
    aim = _smoke_aim(room, machine, seat_index, _now())
    if aim is None:
        return None
    cover = _smoke_cover(room, seat_index, _now())
    index = aim[2]
    # ★ 乱射的那个点打不出去（钻进地里 / 被挡住）就退回**云心**——
    #   真人瞎打也会往那团烟的中间放，不会因为一个点不通就干脆不开枪。
    for tx, ty in ((aim[0], aim[1]),
                   (cover[0], cover[1]) if cover else (aim[0], aim[1])):
        mx, my = _muzzle(x, y, tx)
        span = math.hypot(tx - mx, ty - my)
        if span > BOT_ENGAGE_RANGE:
            continue
        shot = solve(tx - mx, ty - my)
        if shot is None or _outlives_fuse(weapon, shot):
            continue
        if _path_blocked(terrain, mx, my, shot, weapon.size):
            continue
        return Engagement(index, (tx, ty), shot, span, 0.0,
                          _hit_radius(room, index, weapon))
    return None


def _hit_radius(room, seat_index, weapon):
    """命中窗口 = 目标**身体**那个圆 + 弹体自己的半径。

    判命中时收方就是把两边的碰撞半径相加（`0x50f410`），三个圆里身体那个
    最大也最常被打中（§66），拿它当「打得中打不中」的尺度。
    """
    seat = room.seats[seat_index] if 0 <= seat_index < len(room.seats) else None
    character = chrprops.get(0 if seat is None else seat.character_id)
    return character.size_body + float(weapon.size or 0.0)


def _aim_error_chance(room, machine, seat_index):
    """这一发算错提前量的概率 —— 难度那一格（M5-D）。

    ★ 道具赛里别人放的**糊屏**会把它顶上去，那条在 `_hud_jam_bonus()` 里
    （M5-F）；这里只负责把两者合起来，上限 1.0。
    """
    chance = difficulty_profile(room)["aim_error"]
    return min(1.0, chance + _hud_jam_bonus(room, machine, seat_index))


def _aim_miss(room, machine, seat_index):
    """**这一发**的瞄准失误（`botaim.Miss`），没失误返回 `None`。

    掷一次就存着，直到真的打出一发才清（`_try_fire()` 末尾）——
    这样「失误概率」说的是**每一发**，而且准星不会逐帧抖。
    """
    if not machine.aim_miss_rolled:
        machine.aim_miss_rolled = True
        machine.aim_miss = botaim.roll_error(
            machine.roll, _aim_error_chance(room, machine, seat_index))
    return machine.aim_miss


def _reroll_aim_miss(machine):
    """打出一发之后：下一发重新掷。"""
    machine.aim_miss = None
    machine.aim_miss_rolled = False
    # ★ 朝烟里乱射的那个偏移同理 —— 每一发换一个点，才叫「乱射」。
    machine.smoke_offset = None


def _choose_weapon(room, machine, seat_index):
    """**这一帧用哪把枪**（M5-C）。返回 `weapondata.Weapon` 或 `None`。

    优先级（前两条都不是 AI 说了算的）：

    1. 房主 `/w 1..3` **锁枪** —— 只能用那一把，锁的那把打不到就不开枪；
    2. 地上**捡来的**特殊枪 —— 原版就是「换成这把，用完还原」（§115 / §223）；
    3. 剩下才轮到 AI 评分：`botarms.score()` 把每把枪折算成「每秒有效伤害」，
       高出当前那把 `SWITCH_MARGIN` 倍才换（换枪要丢半个弹匣）。

    ★★ 评分只在**打得到人**的时候才有意义：一把枪此刻解不出弹道 / 被地形
    挡住 / 引信先炸，那是「不可用」而不是「分低」，直接不进候选。全都打不到
    （对面躺着、隔着半张图）时**什么都不换** —— 没有新事实就不做新决定。

    ## ★★★ 会话 41：换枪要**算上等待的时间**（§126 的直接后果）

    加了 `LoadingTime` 之后换枪不再是免费的：新拿的那把要上膛（100~2000 ms），
    而这把枪自己冻着的冷却切回来还得走完。实机日志里见过 **3 ms 内换了两次**
    （`1000020 -> 1000010 -> 1000020`）—— 有了上膛时间，这么换的 bot 一枪
    都打不出来。

    ⇒ 比的不再是「每秒有效伤害」，而是「**在打倒他所需的这段时间里，
    哪把枪总共能打出更多伤害**」：`分 × max(0, 剩余时间 − 要等多久)`。
    量纲是伤害，两项都是既有事实（分来自 `botarms`，等待来自那三张倒计时
    表），没有新旋钮。手上这把等待通常是 0 ⇒ 天然占优，抖动自己就消了。
    """
    if machine.weapon_slot is not None or machine.item_weapon is not None:
        return machine.weapon
    options = weapondata.usable_for(machine.character_id)
    if not options:
        return machine.weapon
    terrain = _terrain(room)
    # ★ 和开火 / 走位**同一个门**（`_breaking_now`）：视野里有活敌人或
    #   活怪的时候不为了一件罐子换枪 —— 换枪要丢半个弹匣，人贴脸时
    #   这一下比罐子贵得多。被压住那种「非打不可」它自己会放行。
    blocker = _breaking_now(room, machine, seat_index, terrain)
    if blocker is not None and machine.battle_pos is not None:
        # 破障时的评分是“还要多久打碎”：剩余 HP / 这一发真会造成的
        # 伤害，再按武器自己的弹匣、冷却、换弹和切枪等待算完。
        # `/w` 锁枪和特殊武器已在上面返回，仍然优先于 AI。
        ledger = _breakables(room)
        left = (blocker.hp if ledger is None
                else ledger.hp.get(blocker.index, blocker.hp))
        now = _now()
        timings = {}
        hurts = {}
        x, y = machine.battle_pos
        for candidate in options:
            option = _breakable_option(
                room, machine, candidate, terrain, _solver_for(candidate), x, y)
            if option is None or option[1] <= 0:
                continue
            hurt = option[1]
            shots = max(1, int(math.ceil(float(left) / hurt)))
            wait = _switch_wait(machine, candidate, now)
            magazine = int(candidate.magazine or 0)
            if magazine > 0:
                active = max(0, shots - 1) * float(candidate.cooling_ms or 0)
                reloads = max(0, (shots - 1) // magazine)
                active += reloads * float(candidate.reload_ms or 0)
            else:
                active = (max(0, shots - 1)
                          * float(candidate.fire_interval_ms or 0))
            timings[candidate.id] = wait + active / 1000.0
            hurts[candidate.id] = hurt
        if timings:
            current = machine.weapon
            current_id = None if current is None else current.id
            best_id = min(timings,
                          key=lambda key: (timings[key], key != current_id, key))
            if best_id != current_id:
                machine.auto_weapon_id = best_id
                machine.log(
                    f"   破障换枪: {current_id} -> {best_id}；挡路物 "
                    f"{blocker.handle} 剩余 {left} HP，每发 {hurts[best_id]}，"
                    f"预计 {timings[best_id]:.2f}s 打碎")
            return machine.weapon
    scale = _damage_scale(room)
    ratio = _magazine_ratios(machine)[0]
    current = machine.weapon
    scores = {}
    victims = {}
    for candidate in options:
        option = _engagement(room, machine, seat_index, candidate)
        if option is None:
            continue
        value = botarms.score(candidate, option.shot, option.speed,
                              option.hit_radius, option.span,
                              damage_scale=scale, damage_ratio=ratio)
        if value is not None:
            scores[candidate.id] = value
            victims[candidate.id] = option.seat
    if not scores:
        return current
    now = _now()
    peak = max(scores.values())
    horizon = _kill_horizon(room, scores, victims, peak)
    yields = {}
    for key, value in scores.items():
        wait = _switch_wait(machine, weapondata.get(key), now)
        yields[key] = value * max(0.0, horizon - wait)
    if max(yields.values()) <= 0.0:
        # 窗口比谁的等待都短（对方只剩一口气）—— 总账全是 0，分不出高下，
        # 那就退回原来那把尺子「每秒有效伤害」，别在一堆 0 里瞎挑。
        yields = scores
    best_id = max(yields, key=lambda key: (yields[key], -key))
    current_id = None if current is None else current.id
    if current_id not in yields or botarms.better(
            yields.get(current_id), yields[best_id]):
        if best_id != current_id:
            machine.auto_weapon_id = best_id
            _log_weapon_choice(machine, current, best_id, scores, yields)
    return machine.weapon


def _switch_wait(machine, weapon, now):
    """换到（或者继续用）这把枪，**最早**什么时候能开出第一发（秒）。

    照原版那三张倒计时表算（§126）：手上这一把只剩自己的冷却；
    别的那几把是「冻住的剩余」+ 这把枪的 `LoadingTime`。
    """
    if weapon is None:
        return 0.0
    if machine.declared_weapon == weapon.id:
        return max(0.0, machine.next_fire_at - now)
    return (max(0.0, float(machine.weapon_cd.get(weapon.id, 0.0) or 0.0))
            + float(weapon.loading_ms or 0) / 1000.0)


def _kill_horizon(room, scores, victims, peak):
    """「照最快的打法，把他打倒还要多久」—— 换枪算总账时的时间窗（秒）。

    ★ 血量走的是 M5-C 那本台账（`bothp`），打不到人 / 打的是怪时退回**满血**
    —— 怪的血服务端不知道（句柄映射不回 `Mob.ini`），拿满血当上界即可。
    """
    if peak <= 0.0:
        return 0.0
    seat = None
    for key, value in scores.items():
        if value == peak:
            seat = victims.get(key)
            break
    left = None
    if seat is not None and 0 <= seat < len(room.seats):
        ledger = _health(room)
        maximum = _seat_max_hp(room, seat)
        left = maximum if ledger is None else ledger.remaining(seat, maximum)
    if left is None or left <= 0:
        # 打的是怪（`MOB_SEAT`）/ 台账还没起来 —— 拿满血当上界。
        left = float(chrprops.get(0).hp)
    return max(0.0, float(left)) / peak


def _log_weapon_choice(machine, previous, chosen, scores, yields=None):
    """换枪打一行 —— **按状态翻转去重**（铁律 10 的口径）。"""
    weapon = weapondata.get(chosen)
    if weapon is None:
        return
    table = " ".join("%s=%.1f" % (key, value)
                     for key, value in sorted(scores.items()))
    tail = ""
    if yields:
        tail = ("；这段时间的总伤害 "
                + " ".join("%s=%.0f" % (key, value)
                           for key, value in sorted(yields.items())))
    machine.log(f"   换枪: {getattr(previous, 'id', None)} -> {weapon.id}"
                f"({weapon.raw.get('section', '?')})　每秒有效伤害 {table}"
                f"{tail}")


def _declare_weapon(machine, seat_index, weapon):
    """★ 头一次开火（以及每次换枪）之前，先发一发 `rpChangeWeapon` 声明武器。

    **不发的后果是「看到的枪和打出来的子弹对不上」**：客户端建角色时给的是
    它自己的默认武器，而 bot 的 `rpFire` 里带的是 `weapondata` 挑的那把 ——
    角色 1 / 100 / 103 的首选甚至是 **3 号槽**，别人屏幕上却举着 1 号枪。
    用户 2026-08-26 报的「bot 好像只会用 1 武器」，看到的就是这个。

    ★ **按状态翻转去重**（铁律 10 的口径）：和上次声明的不一样才发，
    一样就什么都不做。`declared_weapon` 在 `reset_battle_frame()` 里跟着清
    —— 换图 / 新一局客户端重建角色，武器回默认，两边的记账必须一起清（§41）。

    ⚠ 这只管**声明**，不管「什么时候该换枪」—— 那是 AI 决策，归 M5。
    """
    if machine.declared_weapon == weapon.id:
        return False
    previous = machine.declared_weapon
    machine.declared_weapon = weapon.id
    _switch_weapon_clock(machine, previous, weapon, _now())
    _emit(machine, machine.sync.event(
        botsync.OP_CHANGE_WEAPON,
        botsync.change_weapon_body(seat_index, weapon.id)))
    return True


def _switch_weapon_clock(machine, previous_id, weapon, now):
    """★★★ 换枪的三件事（V0.3 §126，原版 `0x51727f` / `0x48bd59`）。

    用户 2026-08-30：「bot 换枪后立刻就能开枪，这不合理。真人换武器后有
    两个 cd：一个固定的（1 秒左右），一个是武器原本的 cd；而且**每把武器
    的 cd 是单独计算的**，切走就暂停，切回来接着走完。」

    ## 原版确实就是这么做的（逐指令）

    持枪器里有**三张按武器 id 索引的倒计时表**：`+0x60` = `LoadingTime`、
    `+0x78` = `ReloadTime`、`+0x90` = `CoolingTime`。

    * `0x51727f`（换武器）-> `0x48bcaa(持枪器, 新武器id, LoadingTime)`
      —— 只给**新拿的那把**上「上膛」倒计时；
    * `0x5163fe`（每帧）-> `0x48bd59(持枪器, [持枪器+0x18])`
      —— 键是**当前手上那把** ⇒ **切走的那把三个倒计时全部定格**，
      切回来接着往下走；
    * `0x48f573`（能不能开枪）—— 三张表任意一张 `> 0` 就开不出去。

    ⇒ 「固定 cd」其实是每把枪自己的 `LoadingTime`（基础九把 100~400 ms，
    重武器到 2000 ms），不是一个全局常量。

    ## 这里怎么落地

    服务端只留**手上这一把**的绝对时刻（`next_fire_at` / `rounds_left`），
    切走时把「还剩多少」冻进 `weapon_cd` / `weapon_rounds`，切回来再解冻。
    等价于原版那三张表，只是我们同一时刻只需要一把的精度。
    """
    if previous_id is not None:
        machine.weapon_cd[previous_id] = max(0.0, machine.next_fire_at - now)
        machine.weapon_rounds[previous_id] = machine.rounds_left
    # ★ 切回来：先把冻住的那份剩余解冻，再叠上这把枪的 `LoadingTime`。
    #   两者**取和**而不是取大 —— 原版那三张表是各自独立倒数的，
    #   `LoadingTime` 那一张刚被重新上满，`CoolingTime` 那一张接着走完，
    #   要等到**两张都归零**才开得出枪。
    resume = float(machine.weapon_cd.pop(weapon.id, 0.0) or 0.0)
    loading = float(weapon.loading_ms or 0) / 1000.0
    machine.next_fire_at = now + resume + loading
    # ★ 弹匣也是**跟着枪走**的：切走时剩几发，切回来还是几发。
    machine.rounds_left = machine.weapon_rounds.pop(weapon.id, None)


#: ★ 诊断（`/slow`）的降速倍率。收方的初速是 `power × Velocity`
#: （`0x4920a7`，`PowerControl=0`），所以 `power` 直接当倍率用。
BOT_SLOW_FACTOR = 0.1


def _slow_shot(weapon, shot):
    """把这一发降到 `BOT_SLOW_FACTOR` 倍初速（诊断用，`/slow`）。

    ★ 抛物线武器的**落点会偏**（角度没跟着重解）—— 这个开关是拿来
    **看轨迹**的，要配合 `/noboom` 用，不看命中。
    """
    factor = BOT_SLOW_FACTOR
    speed = shot.speed * factor
    if weapon.power_control == ballistics.MODE_PLAIN:
        power = factor
    else:
        power = ballistics.power_for_speed(weapon, speed)
    return ballistics.Shot(shot.angle, power, speed, shot.ticks / factor,
                           shot.gravity, shot.accel, shot.cap)


def _diag_why_not_firing(room, machine, seat_index, weapon, target, now):
    """★★ **临时诊断**（会话 17，跟 `BOT_DIAG_FIRE_ANYWHERE` 一起删）：
    这一帧 bot 为什么没开枪。

    **按状态翻转去重**（铁律 10 的口径）：原因和上一次一样就什么都不打，
    真的换了原因才打一行 —— 所以 8 Hz 的帧率不会把日志刷爆。
    """
    if weapon is None:
        why = "没有武器（weapondata 没给它挑出一把）"
    elif machine.battle_pos is None:
        why = "没有 battle_pos（这一局还没被放进地图）"
    elif target is None:
        hostiles = list(_visible_targets(room, machine, seat_index))
        if not hostiles:
            why = "视野里没有敌人（都在屏幕外 / 队伍分边 / 位置还不知道？）"
        else:
            spans = []
            x, y = machine.battle_pos
            for index, tx, ty, _crouched in hostiles:
                spans.append(f"座位{index}={math.hypot(tx - x, ty - y):.0f}")
            why = ("挑不出目标（弹道解不出来 / 被地形挡住）；"
                   f"我在 {x:.0f},{y:.0f} 距离 " + " ".join(spans))
    elif now < machine.next_fire_at:
        why = f"还在冷却，差 {machine.next_fire_at - now:.2f}s"
    elif not _may_fire(machine, weapon):
        why = f"上一发的爆炸还没发完（在飞 {len(machine.pending_shots)} 颗）"
    else:
        why = None                      # 这一帧真的开了枪
    if why == machine.diag_last_why:
        return
    machine.diag_last_why = why
    if why is not None:
        machine.log(f"   ◆诊断 不开枪：{why}")


def _may_fire(machine, weapon):
    """现在允许开下一枪吗（句柄记账的**顺序闸门**）。

    ★★ 带溅射的武器每颗子弹**多吃一个句柄**（§43），而那一个到底是
    开火时分配的还是爆炸时分配的，语料分不出来。两种假设只有在
    「**上一发的 `rpExplode` 全发完之前不开下一枪**」时才给出同一个答案 ——
    所以对这类武器加一道闸门。

    代价几乎为零：带溅射的武器开火间隔全在 1000 ms 以上，而飞行时间
    最长也就 1 秒出头。直射无溅射的武器 `handle_step == shots`，全部分配
    都发生在开火那一刻，怎么交错都对，**不设闸**。
    """
    return not (weapon.splash_range and machine.pending_shots)


def _reload_after_shot(machine, weapon, now):
    """打完这一发之后，下一发**最早**什么时候能打。全部取自 `weapon.ini`。

    ★★ **弹匣是原版节奏的一半**（用户 2026-08-26 报的）：

        连打 `MagazineCount` 发，每发之间隔 `CoolingTime`；
        打空了停 `ReloadTime` 换弹匣，再装满。

    只看 `CoolingTime` 的话 bot 的持续输出是真人的好几倍 ——
    角色 1 的 1 号枪 `CoolingTime=200 / MagazineCount=2 / ReloadTime=1200`：
    原版是「两发 + 1.2 秒」= 1.4 秒 2 发，只看冷却就是 1.4 秒 **7 发**，
    而它还是三连散弹（一发 3 颗 × 3 伤）⇒ 秒人。

    ★ 没有 `MagazineCount` 的武器（榴弹 / 火箭那一类，打一发装一次）
    走 `fire_interval_ms`（= `CoolingTime` 或 `ReloadTime`），和原来一样。

    ★ 这几个数**全是原版数据**，不是我拿一台机器观测出来的阈值 ——
    铁律 10 禁的是后者（D29 的口径）。
    """
    magazine = weapon.magazine
    if not magazine:
        machine.rounds_left = None
        return now + weapon.fire_interval_ms / 1000.0
    left = magazine if machine.rounds_left is None else machine.rounds_left
    left -= 1
    if left <= 0:
        # 打空了：停下来换弹匣，回来就是满的。
        machine.rounds_left = None
        return now + (weapon.reload_ms or weapon.fire_interval_ms) / 1000.0
    machine.rounds_left = left
    return now + (weapon.cooling_ms or weapon.fire_interval_ms) / 1000.0


# ---------------------------------------------------------------------------
# ★★★ 命中判定（§65）—— 服务端自己把弹道跑一遍
# ---------------------------------------------------------------------------
#: 弹体最多飞多远（世界单位）就算「没了」。
#:
#: 有地形数据时用**这张图自己的对角线**（飞出图外客户端本来就销毁弹体）；
#: 没有地形数据时退回这个数 —— 它比 174 张图里最大的那张（11400 × 4500）
#: 的对角线还长。★ 这是**几何上界**（图有多大），不是「飞多久算超时」。
BOT_SHELL_MAX_TRAVEL = 12288.0

#: ★★ 判「这一 tick 有没有撞地形」时沿线段采样的步长 —— **1 个像素**（§79）。
#:
#: 收方是**逐像素**走的：`0x47f8a5` 把上一帧和这一帧的位置取整，
#: `0x47f912` 起在两点之间一格一格插值，每一格问一次 `TerrainData::Get`
#: （`0x472fe0`），值 2/3 就算撞上（`0x47f976`）。中间**一个像素都不跳**。
#:
#: 这里原来跟着 `BOT_LINE_STEP`（4）走，于是**擦边**的那些发就漏判了：
#: 弹体半径才 8，平台边缘只要窄于 4 个像素就整个从采样点之间穿过去
#: —— 收方把弹体停在平台边上，服务端却让它一路飞到下面的地面才炸
#: （用户 2026-08-28 报的「手雷擦到平台右边缘，结果穿过去在地上炸开」）。
#:
#: ⚠ 别拿它和 `BOT_LINE_STEP` 合并：那一个是**开枪前**问「这条线通不通」，
#: 采样粗一点只影响 bot 要不要开这一枪；这一个是**结算**，
#: 和收方差一个像素就是爆炸点对不上。
BOT_SHELL_TERRAIN_STEP = 1


class Shell(object):
    """一颗**在飞的**子弹 —— 服务端这边的那一份。

    收方每个 tick（32 ms）把弹体推进一步、当场判碰撞（`0x47de6a` →
    `0x480420`），撞上就把弹体标记成结束（`0x481178`）。但**算伤害、发
    `rpExplode` 的只有射手那台机器**（`0x47eb4e` 的 `IsMine || IsNeutral`
    守卫）—— bot 没有本机，所以那套判定归服务端（D28 / §42）。

    ★★ 会话 18 之前这里根本没有判定：`_impact_point()` 一律把爆炸点搬到
    「目标此刻站的地方」并报命中，于是**百发百中**、玩家明明躲开了照样
    掉血，火箭飞到地上炸了爆炸特效却出现在玩家身上（用户 2026-08-27 报的
    三条症状全是它）。现在这个类逐 tick 跑真弹道，撞到什么就是什么。

    ⚠ **每一颗都必须恰好发一发 `rpExplode`**：句柄记账在开火那一刻就
    推进了，少发一发收方那一格计数器就和服务端错开，从此打不掉血（§42）。
    所以 `_advance_shells()` 里没有任何一条「算了这颗不管了」的路。
    """

    __slots__ = ("handle", "fire_seq", "weapon", "group", "x0", "y0",
                 "shot", "born", "born_tick", "ticks", "x", "y",
                 "max_ticks",
                 "vx", "vy", "locked", "bounced",
                 "damage_ratio", "size_ratio")

    def __init__(self, handle, fire_seq, weapon, group, x0, y0, shot, born,
                 max_ticks, born_tick=0):
        self.handle = int(handle)
        #: 开火那一发 `rpFire` 的事件序号 —— 换代之后拿它认出「这是上一代的」。
        self.fire_seq = int(fire_seq)
        self.weapon = weapon
        #: 碰撞排除组（§63）。判命中时和对方的组一比，相同就穿过去。
        self.group = int(group)
        #: 枪口（发射点），闭式解的原点。
        self.x0 = float(x0)
        self.y0 = float(y0)
        self.shot = shot
        self.born = float(born)
        #: ★★★ **出膛在本局第几个 32 ms 格子上**（D106）。收方那份弹体的
        #: 时钟锚在它收到 `rpFire` 的那一帧，我们这份锚在发出的那一格 ——
        #: 两边都是「此后每 32 ms 推一格」，所以 `rpExplode` 的发出时刻
        #: 恒等于「出膛 + k×32 ms」，网络延迟对两发是同一份、自动抵消（§147）。
        self.born_tick = int(born_tick)
        #: 已经推进了几个**收方 tick**（32 ms 一个）。
        self.ticks = 0
        self.x = float(x0)
        self.y = float(y0)
        self.max_ticks = int(max_ticks)
        # ★★ **追踪弹**（`HomingAngle > 0`）走的是逐 tick 积分那条路（§77）：
        #    弹道会拐弯，闭式解表达不了。这两格是**当前**速度矢量，
        #    非追踪弹恒不用。
        self.vx = math.cos(shot.angle) * shot.speed
        self.vy = math.sin(shot.angle) * shot.speed
        #: 锁上的目标座位；`None` = 还没锁上。★ 原版**锁上就不换**
        #: （`[proj+0x328]` 一旦不是 −1 就直接跳过选目标那一段，`0x47e36b`）。
        self.locked = None
        #: ★★ **已经在地上弹过了**（带引信的武器，§84）。弹过之后弹道
        #:   不再是闭式解，改走 `vx/vy` 逐 tick 积分那一路。
        self.bounced = False
        #: ★★ 开火那一刻射手身上那些**按发数算的状态**给的倍率（§117）。
        #:   强力射击是伤害 ×2、弹体大小 ×2。在**开火那一刻**定死，
        #:   不跟着状态到期变 —— 这一颗已经飞出去了。
        self.damage_ratio = 1.0
        self.size_ratio = 1.0

    @property
    def radius(self):
        """这一颗**实际**的碰撞半径 = `Size × SizeRatio`（§116 / §117）。

        ★ 判地形、判命中、判弹跳台一律用它，不要直接读 `weapon.size`
        —— 强力射击那三发在每台客户端上都是两倍大的。
        """
        return self.weapon.size * self.size_ratio

    def position(self, ticks):
        return ballistics.position_at(self.x0, self.y0, self.shot, ticks)

    def __repr__(self):
        return ("<Shell %d 武器%s 组%d %d/%dtick (%.0f, %.0f)>"
                % (self.handle, getattr(self.weapon, "id", "?"), self.group,
                   self.ticks, self.max_ticks, self.x, self.y))


#: 循环上界的**硬顶**（tick）。谁都不该走到这儿 —— 图外算实心（`cell()`
#: 出界返回 2），所以任何弹体迟早会在图的边界上撞住。留着只是防死循环。
BOT_SHELL_TICK_CEILING = int(ballistics.TICKS_PER_SECOND * 20)


def _shell_max_ticks(terrain, shot, weapon=None):
    """这颗子弹最多推进几个 tick —— 走完 `BOT_SHELL_MAX_TRAVEL` 就到头。

    ★ 循环上界，不是超时阈值：飞出图外的弹体客户端自己就销毁了，
    服务端这边也得有个头，否则一颗打空的直射弹会永远挂在「在飞」队列里，
    把带溅射武器的那道顺序闸门（`_may_fire`）永久卡死。

    ★★ 带**引信**的武器另有一条更早的上界：收方在第 `fuse_ticks` 个 tick
    上就把弹体炸掉了（§72），过了那一刻服务端再算下去也没有意义 ——
    对面已经没有这个弹体了。

    ## ★★★ 除的必须是**最慢**的那一刻的速度（§83）

    这里原来除的是 `shot.speed`（**出膛**速度）。抛物线弹在顶点的水平
    速度只有 `speed × cosθ`，高抛的时候只剩三分之一 —— 于是上界比真正
    的飞行时间**短一大截**，弹体在半空中就被「飞到头了」结算掉。

    实机对上的那一发（2026-08-28 的 `logs/server.out`，弹体句柄 200048）：
    `speed=35.6 / 25 tick` 的火焰弹，上界算出来 56 tick，服务端在
    第 56 tick 把它炸在 `(1608.2, 236.8)`；而客户端逐帧日志里它那时候
    是第 57 帧、正在 `(1608.16, 236.76)` **继续往下飞**，离落地还有十几帧。
    用户看到的就是「手雷飞一半突然炸了 / 消失了」。

    ⇒ 分母换成整条弹道上**最小**的速率：有重力时是 `|vx|`（顶点那一刻
    竖直分量为 0），没重力时就是 `speed` 本身。再夹一个硬顶防死循环。
    """
    travel = BOT_SHELL_MAX_TRAVEL
    if terrain is not None:
        travel = math.hypot(terrain.width, terrain.height)
    speed = max(1e-3, shot.speed)
    if shot.gravity:
        # 顶点那一刻只剩水平分量 —— 整条抛物线上最慢的就是它。
        speed = max(1e-3, abs(shot.speed * math.cos(shot.angle)))
    ticks = max(1, int(math.ceil(travel / speed)))
    ticks = min(ticks, BOT_SHELL_TICK_CEILING)
    fuse = None if weapon is None else weapon.fuse_ticks
    if fuse:
        ticks = min(ticks, max(1, fuse - 1))
    return ticks


def _segment_circle_t(ax, ay, bx, by, cx, cy, radius):
    """线段 A→B **第一次**进入圆 `(C, radius)` 的参数 `t ∈ [0, 1]`。

    不相交返回 `None`；起点就在圆里返回 `0.0`。

    ★ 为什么是**线段**而不是「这一 tick 的落点」：直射枪一个 tick 走 100
    个单位，而人的身体圆直径才 26~36 —— 按落点采样的话子弹会**从人身上
    穿过去**（隧穿）。原版的碰撞是「一串形状 × 一串形状」求交
    （`0x50f410`），直射弹在画面上本来就是一条线（§45），所以扫掠段
    才是对的模型。
    """
    dx = bx - ax
    dy = by - ay
    fx = ax - cx
    fy = ay - cy
    if fx * fx + fy * fy <= radius * radius:
        return 0.0
    a = dx * dx + dy * dy
    if a <= 0.0:
        return None
    b = 2.0 * (fx * dx + fy * dy)
    c = fx * fx + fy * fy - radius * radius
    disc = b * b - 4.0 * a * c
    if disc < 0.0:
        return None
    t = (-b - math.sqrt(disc)) / (2.0 * a)
    return t if 0.0 <= t <= 1.0 else None


def shell_probe_offsets(radius, dx, dy):
    """收方拿**哪几个点**去查地形（V0.3 §116）。返回整数格偏移的元组。

    ## 这是逆出来的，不是拟合的

    扫掠函数 `0x50e759` 每走到线上一格，就拿一组**偏移**去查那一格
    （`0x50ea43` 的 `linePoint + offset[j]`）。偏移只有两种来源：

    ```text
    offset[0] = ftol([vft+0x104])                       ← 0x50e904..0x50e935
                弹体类一律 0x47c7d3 = (0.0, [vft+0x7c]) = (0, 半径)
    offset[n] = ftol(形状中心 + 半径 × 单位速度矢量)      ← 0x50e98c..0x50e9b0
                fld [shape+0x18]        ; 半径
                fmul [ebp-0x50/-0x4c]   ; × 单位速度的 x / y
                fadd [shape+0x10/+0x14] ; + 形状中心
    ```

    ⇒ **是沿飞行方向前伸的一个点 + 正下方一个点，不是一个圆盘。**

    ## ★★ 这一条推翻了 §83 的「圆盘」

    §83 只看到「停下那一点离最近实心格恰好一个 `Size`」，就把形状拟合成
    半径 `Size` 的**实心圆盘**。可圆盘是**各向同性**的，而这个采样点组是
    **朝前**的 —— 两者在「贴着侧面飞过去」时结论相反：

    | | 圆盘 r=Size | 鼻尖 + 正下方（原版）|
    |---|---|---|
    | 客户端明明飞过去了、模型说撞上（5312 个样本）| **102** | **2** |
    | 客户端撞上了、模型没拦住（5 次真反弹）| 0 | **0** |

    样本是 2026-08-29 16:17 那一局 `Iceria_b` 的客户端逐帧日志 `PROJ.`
    （21 条弹道）。**102 次误判**就是用户报的「苹果弹从平台扔到下层地面，
    反弹后落点和炸点不一致」：那一发（句柄 200193）在服务端第 7 tick 撞上了
    右边 6.7 个单位外的斜坡、当场弹开往左走，客户端那颗从旁边擦过去继续下落
    —— 到爆炸时两颗差了 **264 个单位**（服务端炸在 `(24.75, 814.86)`，
    玩家看见的那颗在 `(286, 852)`）。
    """
    offsets = [(0, int(radius))]
    span = math.hypot(dx, dy)
    if span > 1e-9:
        offsets.append((int(radius * dx / span), int(radius * dy / span)))
    return tuple(offsets)


def _probe_blocked(terrain, x, y, offsets):
    """这一组采样点里有没有一个落在挡子弹的地形上。

    ★★ **图顶上面（`y < 0`）不算实心**（§83）。`TerrainData::Get`（`0x472fe0`）
    对出界一律返回 2，可实机逐帧日志里弹体是**从图顶飞出去又落回来**的
    （句柄 200048 第 36 帧在 `(1323.48, 0.68)`，采样点早该顶到 `y = −7` 了，
    它照飞不误）。把顶上算成实心的话，高抛的手雷会在半空中被判撞墙 ——
    那两发实测差了 400 / 495 个单位。左右和底下照旧算实心。
    """
    blocks = terrain.blocks_bullet
    for ox, oy in offsets:
        yy = y + oy
        if yy >= 0 and blocks(x + ox, yy):
            return True
    return False


def _terrain_contact(terrain, ax, ay, bx, by, radius=0.0):
    """线段 A→B 上第一次**撞地形**的 `(撞上那一点的 t, 撞上之前最后一点的 t)`。

    一路通畅返回 `(None, None)`。起点本身就撞着返回 `(0.0, 0.0)`。

    用 `blocks_bullet()` 那一路 —— 单向平台（值 1）挡人不挡子弹（§29）。

    ## ★★★ 弹体是**有粗细**的，不是一个点（§83 / ★ 形状按 §116 收口）

    这里原来只查**圆心**那一格，于是「圆心还在空中、边缘已经蹭上平台」的
    那些发服务端全部放行、一路飞下去，在几百个单位以外才炸（用户报的
    「手雷空中飞一半突然消失，过一会儿在地图边缘炸」）。

    ⚠ **粗细的形状是「前伸的一个点 + 正下方一个点」，不是圆盘**
    —— 见 `shell_probe_offsets()`。圆盘那一版会在「贴着侧面飞过去」时
    误判成撞上，5312 个客户端样本里错 102 次。

    ⚠ 反弹要用**撞上之前**那一点（收方也是把弹体夹回最后一个不重叠的
    采样点），否则弹体贴在地形里，下一 tick 一开头又撞上，原地卡死。

    ## ★★★ 逐像素扫**没变**，只是空气整块跳过（用户 2026-09-01 的掉帧）

    实测：空中飞 300 像素的一颗弹，逐格问要 **270 µs**，而每颗在飞的弹每
    32 ms 都要算一次 —— 10 颗在飞就吃掉 2.7 ms / 32 ms，30 颗就是 8 ms。
    子弹一多整个房间的 tick 就跟不上，`[sim] ⚠ 落后 N 格` 就是这么来的。

    加速的办法是 `mapdata.MapTerrain.bullet_coarse()` 那张**粗网格**
    （每 16×16 一块，记「这块里有没有挡子弹的格子」）：采样点落在空块里
    ⇒ 它在这块里还能走的那几步全都不用问，直接跳过去。

    ★ **结果和逐像素扫逐位一致**，不是近似：跳几步是算出来的，不是猜的。
      每走 j 步，坐标的位移是 `δ = j × span / steps`；而
      `|floor(u+δ) − floor(u)| ≤ floor(δ) + 1`，所以只要 `δ < 离块边的像素数`
      就一定还在同一块里。展开就是 `j × span < margin × steps`，全整数比较，
      不吃浮点误差。原实现原样留着，叫 `_terrain_contact_exact()`，
      差分测试拿它当基准（`test_ballistics.py`）。

    ★ 出界和「图顶上面」这两种口径特殊的，一律退回逐格那条 —— 它们很少见，
      不值得为它们把网格搞复杂。
    """
    if terrain is None:
        return (None, None)
    dx = bx - ax
    dy = by - ay
    if radius and radius >= 1.0:
        offsets = shell_probe_offsets(radius, dx, dy)
    else:
        offsets = ((0, 0),)
    if _probe_blocked(terrain, int(ax), int(ay), offsets):
        return (0.0, 0.0)
    span = max(abs(dx), abs(dy))
    steps = max(1, int(span // BOT_SHELL_TERRAIN_STEP))
    grid, gw, _gh = terrain.bullet_coarse()
    # 全部提成局部变量：这是**每一步**都要跑的循环，属性查找的账付不起。
    xmax = terrain.width - 1
    ymax = terrain.height - 1
    shift = mapdata.COARSE_SHIFT
    edge = mapdata.COARSE - 1
    # 走向：只用来判「离块边还有多远」该往哪边量。
    sx = 1 if dx > 0 else (-1 if dx < 0 else 0)
    sy = 1 if dy > 0 else (-1 if dy < 0 else 0)
    adx = abs(dx)
    ady = abs(dy)
    far = steps + 1                     # 「这个轴不会离开这一块」的哨兵
    i = 1
    while i <= steps:
        t = float(i) / steps
        cx = int(ax + dx * t)
        cy = int(ay + dy * t)
        # ★★ x 和 y 的余量**分开记**，别取 min 之后拿 span 一起换算：
        #    两轴的步进速度差多少倍，余量能换成的步数就差多少倍。
        #    合起来算的话，斜率 6:1 的那种平射弹会被慢轴白白拖住 ——
        #    实测平均只能跳 1.8 步，分开之后跳 8 步以上。
        mx = far
        my = far
        for ox, oy in offsets:
            yy = cy + oy
            if yy < 0:
                # 图顶上面不算实心（§83），而且**和 x 无关**
                # （`_probe_blocked` 先判 `yy >= 0`）。往下走的话它还能安全走
                # `-1 - yy` 步；往上或不动就永远安全。
                room = far if sy <= 0 else (-1 - yy)
                if room < my:
                    my = room
                continue
            xx = cx + ox
            if xx < 0 or xx > xmax or yy > ymax:
                mx = 0                      # 出界：口径特殊，退回逐格
                break
            if grid[(yy >> shift) * gw + (xx >> shift)]:
                mx = 0                      # 这一块里有东西，老老实实问
                break
            # ★★ 「还能走多远」要同时受**块边**和**图边**两个约束。
            #    只看块边是不够的：图宽不是 16 的整数倍时最后一列块是残缺的
            #    （`Beginner` 宽 1800，最后一块管到 x=1807），而 1800..1807
            #    是**出界 = 实心**，网格里却没有它 —— 只看块边就会一步跳过
            #    真正的撞击点（差分测试逮到的正是这个）。
            if sx > 0:
                room = (xx | edge) - xx
                if xmax - xx < room:
                    room = xmax - xx
            elif sx < 0:
                room = xx - (xx & ~edge)
                if xx < room:
                    room = xx
            else:
                room = far
            if room < mx:
                mx = room
            if sy > 0:
                room = (yy | edge) - yy
                if ymax - yy < room:
                    room = ymax - yy
            elif sy < 0:
                room = yy - (yy & ~edge)
                if yy < room:
                    room = yy
            else:
                room = far
            if room < my:
                my = room
        if mx > 0 and my > 0:
            # 每轴各算「再走 j 步仍在同一块里」的上限（推导见 docstring）：
            #   x 轴要 j × |dx| < mx × steps，y 轴要 j × |dy| < my × steps
            jump = steps
            if adx:
                limit = mx * steps
                j = int(limit / adx)
                while j and j * adx >= limit:
                    j -= 1
                if j < jump:
                    jump = j
            if ady:
                limit = my * steps
                j = int(limit / ady)
                while j and j * ady >= limit:
                    j -= 1
                if j < jump:
                    jump = j
            i += jump + 1
            continue
        if _probe_blocked(terrain, cx, cy, offsets):
            return (t, float(i - 1) / steps)
        i += 1
    return (None, None)


def _terrain_contact_exact(terrain, ax, ay, bx, by, radius=0.0):
    """`_terrain_contact()` 加速之前那一版 —— **逐像素，不跳**。

    留着只有一个用途：给差分测试当基准（`test_ballistics.py`）。
    战斗路径上一次都不该调它。
    """
    if terrain is None:
        return (None, None)
    dx = bx - ax
    dy = by - ay
    if radius and radius >= 1.0:
        offsets = shell_probe_offsets(radius, dx, dy)
    else:
        offsets = ((0, 0),)
    if _probe_blocked(terrain, int(ax), int(ay), offsets):
        return (0.0, 0.0)
    span = max(abs(dx), abs(dy))
    steps = max(1, int(span // BOT_SHELL_TERRAIN_STEP))
    for i in range(1, steps + 1):
        t = float(i) / steps
        if _probe_blocked(terrain, int(ax + dx * t), int(ay + dy * t), offsets):
            return (t, float(i - 1) / steps)
    return (None, None)


def _terrain_stop_t(terrain, ax, ay, bx, by, radius=0.0):
    """线段 A→B 上第一个**挡子弹**的采样点的参数 `t`；一路通畅返回 `None`。

    ★★ **起点本身也算**（返回 `0.0`）：和 `_segment_circle_t()`「起点就在
    圆里返回 0.0」是同一个口径。弹体在地形里出生时收方是**原地卡住、
    一步不动**的（§76 的客户端逐帧实测 —— 速度每帧对折还反向，位置一动
    不动），服务端也得当场结算在那儿，不能当它飞走了。
    """
    return _terrain_contact(terrain, ax, ay, bx, by, radius)[0]


#: ★★ 追踪弹每 tick 最多转多少 —— `HomingAngle × 1/7` **度**（§77）。
#:
#: 出处 `0x47e53a` 那三句：
#:
#:     fild [weapondef+0x78]     ; HomingAngle（`ch01-03` = 30）
#:     fmul [0x693c34]           ; = 0.142857149 = 1/7
#:     fmul [0x693778]           ; = π/180
#:
#: 客户端逐帧日志实测：`ch01-03` 的火箭锁上目标之后**每 tick 恒转 4.286°**，
#: 而 `30 / 7 = 4.2857` —— 一位不差。
HOMING_TURN_FACTOR = 1.0 / 7.0

#: ★★ 「目标丢了」的哨兵，照抄收方那一格写的 **−2**（§80）。
#:
#: `0x47e411: mov dword ptr [edi], 0xfffffffe` —— 目标句柄查不到、或者这一
#: tick 量出来已经出了 `HomingRange`，收方就把锁定格写成 −2。而重新选目标
#: 那一段的入口是 `cmp [edi], -1`（`0x47e36b`），−2 进不去
#: ⇒ **这颗弹从此再也不追踪，走直线到底**。
HOMING_LOST = -2


def _homing_target(room, shell, bodies):
    """这一 tick 锁得上谁：`HomingRange` 之内**最近**的那个；没有返回 `None`。

    ★ 原版锁上就不换（`0x47e36b`：`[proj+0x328]` 不是 −1 就跳过选目标）。
    """
    reach = shell.weapon.homing_range or 0.0
    if reach <= 0.0:
        return None
    best = None
    for seat_index, px, py, crouched, character_id in bodies:
        cx, cy = chrprops.get(character_id).center(px, py, crouched)
        span = math.hypot(cx - shell.x, cy - shell.y)
        if span <= reach and (best is None or span < best[0]):
            best = (span, seat_index)
    return None if best is None else best[1]


def _homing_step(room, shell, bodies):
    """追踪弹的一个 tick：先转向、再前进，返回新的落点（§77）。

    和普通弹体的区别只有「速度矢量会拐弯」—— 速度**大小不变**，方向每
    tick 朝目标转最多 `HomingAngle / 7` 度。重力照加（追踪弹现在都是
    `GravityFactor = 0`，加不加都一样，但别把这条漏了）。

    用户 2026-08-28 报的「火箭的飞行轨迹和最终爆炸动画的地点不一样，
    看着撞墙了却在墙的附近另一处爆炸」就是这条没建模：服务端按直线算，
    收方却把它拐走了。

    ## ★★ 目标丢了就**永远**不再追（§80）

    收方每个 tick 都重新量一次「目标还在不在 `HomingRange` 里」
    （`0x47e45b` 的 `fcomp [weapondef+0x7c]`），一旦出圈 / 目标没了，
    它把锁定格写成 **−2**（`0x47e411: mov [edi], 0xfffffffe`）——
    而选目标那一段的入口条件是「锁定格 == −1」（`0x47e36b`），
    所以 −2 之后**再也不会重新选目标**，这颗火箭从此走直线。

    以前服务端锁上就一路跟到底，目标一跑出 200 两边就分家 ——
    用户 2026-08-28 说的「基本吻合了，但偶尔还是不一样」剩下的就是它。
    """
    if shell.locked is None:
        # 还没锁上 —— 和收方一样**每个 tick 都重新扫一遍**，扫不到就下一 tick 再扫
        # （`[proj+0x328]` 停在 −1，`0x47e3e4` 那一跳直接出去，不算「丢了」）。
        shell.locked = _homing_target(room, shell, bodies)
    target = None
    if shell.locked is not None and shell.locked != HOMING_LOST:
        for seat_index, px, py, crouched, character_id in bodies:
            if seat_index == shell.locked:
                center = chrprops.get(character_id).center(px, py, crouched)
                # ★ 每 tick 复查射程 —— 出圈就是「丢了」，永久不再追。
                if math.hypot(center[0] - shell.x, center[1] - shell.y) \
                        <= (shell.weapon.homing_range or 0.0):
                    target = center
                break
        if target is None:
            shell.locked = HOMING_LOST
    if target is not None:
        speed = math.hypot(shell.vx, shell.vy)
        want = math.atan2(target[1] - shell.y, target[0] - shell.x)
        have = math.atan2(shell.vy, shell.vx)
        turn = math.radians(shell.weapon.homing_angle * HOMING_TURN_FACTOR)
        delta = _wrap_angle(want - have)
        if delta > turn:
            delta = turn
        elif delta < -turn:
            delta = -turn
        have += delta
        shell.vx = math.cos(have) * speed
        shell.vy = math.sin(have) * speed
    shell.vy += shell.shot.gravity
    return (shell.x + shell.vx, shell.y + shell.vy)


def _wrap_angle(angle):
    """把角度折回 `(-π, π]`。"""
    while angle <= -math.pi:
        angle += 2.0 * math.pi
    while angle > math.pi:
        angle -= 2.0 * math.pi
    return angle


#: ★★ 撞地形之后速度**打对折**（§84）。
#:
#: 客户端逐帧日志量的（2026-08-28，Forest_b，6 次撞地形）：撞上那一帧
#: 之后 `|v|` 全部正好是撞上之前（**加过这一 tick 重力**的）那一刻的一半。
#: 地图右边界那两次连方向都对得死死的 —— `v=(22.28, 12.50)` 撞上竖直的
#: 图边之后是 `(-11.14, 6.25)`：x 取反、y 不动，再整体减半。
BOUNCE_RESTITUTION = 0.5


#: ★★★★★ 撞了地形怎么办 —— **按弹体类分档**（V0.3 §111，★ 修正 §84）。
#:
#: `BulletObj::OnBlocked`（`0x47eda1`，虚表槽 `+0xa8`，由 `0x50d404` 在
#: 「这一步走不动」时调）第一件事是把位置夹回命中结构里的接触点，
#: 然后 `call [vft+0x160]` 拿一个 **0~4 的档位**分流（`0x47ee40` 那串 `dec eax`）：
#:
#: | 档 | 干什么 | 哪些类（虚表槽 `+0x160` 直接读出来的） |
#: |---|---|---|
#: | 0 | **当场炸**（`[vft+0x15c]` = `Projectile::OnHit`）| `BulletObj`（`0x47d487` 返回 0）、`FlameBomb`、★`SliceBullet`、`ThrowerFlame`、`ThrowerWater`、`TotemLauncher` |
#: | 1 | **弹开** | `AppleGrenade`、`SeedBomb`、`TimeBomb`、`SoldierGrenade`、`RasTurret`（`0x4f167c` 返回 1）|
#: | 2 | **看角度**：`2×|sx| ≤ sy` 就炸，否则弹开（`0x47eec1`~`0x47eede`）| `FlamingBottle`、`TrainingGrenade`（`0x51b052` 返回 2）|
#: | 3 | **钉住**（速度清零，`0x47ee6d`）| `MineBomb`、`PlasmaCannon`、`SpiralKnife`（`0x48528f` 返回 3）|
#: | 4 | 什么都不做，接着飞 | 没有武器用 |
#:
#: ★★ 这**推翻了 §84 的判据**（「有没有 `SliceTime`」）：
#: `SliceBullet` 有引信却是 0 档（撞地就炸），`SoldierGrenade` / `TimeBomb`
#: 没引信却会弹。§84 量出来的「苹果雷落空恒在 11~13 发心跳炸」仍然成立
#: —— 那是引信的效果，不是「会不会弹」的判据。
#: ★ 火焰弹（`FlamingBottle`）落空分布很散那条也对上了：它是 2 档，
#: 平地上 `sx≈0` ⇒ 炸；撞陡壁才弹。
#:
#: `weapon.ini` 里认不出来的 `CreatingClass`（`Grenade` / `Flame` 这些工厂里
#: 没有的名字）会退回基类 `BulletObj` —— 实机验过：`ch00-02a`
#: （`CreatingClass=Grenade`）的弹体虚表就是 `0x66D004` = `BulletObj`。
#: ⇒ 表里查不到的一律 **0 档**。
BLOCKED_EXPLODE = 0
BLOCKED_BOUNCE = 1
BLOCKED_BOUNCE_IF_STEEP = 2
BLOCKED_STICK = 3

BLOCKED_MODE_BY_CLASS = {
    "AppleGrenade": BLOCKED_BOUNCE,
    "SeedBomb": BLOCKED_BOUNCE,
    "TimeBomb": BLOCKED_BOUNCE,
    "SoldierGrenade": BLOCKED_BOUNCE,
    "RasTurret": BLOCKED_BOUNCE,
    "FlamingBottle": BLOCKED_BOUNCE_IF_STEEP,
    "TrainingGrenade": BLOCKED_BOUNCE_IF_STEEP,
    "MineBomb": BLOCKED_STICK,
    "PlasmaCannon": BLOCKED_STICK,
    "SpiralKnife": BLOCKED_STICK,
    # ⚠ 这三个的档位是**每颗弹体自己带的**（`[this+0x350]` / `[this+0x344]` /
    #   `[this+0x33c]`，构造时从武器定义里读），不是常量。武器表里现在
    #   一把都没用到它们，真用上了再逆那三格。
    #   "BounceBullet" / "BoundBall" / "BowlingBullet"
}


def _blocked_mode(weapon):
    """这把武器的弹体撞了地形走哪一档（§111）。认不出来的按 0（当场炸）。"""
    return BLOCKED_MODE_BY_CLASS.get(
        getattr(weapon, "creating_class", None), BLOCKED_EXPLODE)


def _bounces_off_terrain(weapon):
    """这把武器的弹体撞了地形**不会当场炸**吗（§111）。

    「弹开」「钉住」「接着飞」都算 —— 判据是「这一发**没结束**」。
    2 档（火焰弹那种「看角度」）在这里算 True，真正炸不炸由
    `_resolve_terrain_block()` 拿到法线之后再定。
    """
    return _blocked_mode(weapon) != BLOCKED_EXPLODE


def _shell_velocity(shell):
    """这颗弹体**此刻**的速度矢量。

    弹过 / 会拐弯的走 `vx/vy`；还在闭式解上的按 `v = v₀ + g·n` 现算
    （`position_at` 的递推就是这么来的）。
    """
    if shell.bounced or shell.weapon.homing_angle:
        return (shell.vx, shell.vy)
    speed = shell.shot.speed
    return (speed * math.cos(shell.shot.angle),
            speed * math.sin(shell.shot.angle) + shell.shot.gravity * shell.ticks)


#: ★★★★★ 量地形朝向时那个窗口有多大 —— **原版是 7×7**（V0.3 §110）。
#:
#: `0x473b36(世界, x, y)` 整个函数就这么几句（`0x473b43` 起 `push -3; pop ebx`，
#: 两层循环 `0x473b86` / `0x473b8f` 都是 `cmp …, 3; jle`）：
#:
#:     sx = sy = 0
#:     for dy in -3..3:
#:         for dx in -3..3:
#:             if 格子(x+dx, y+dy) 非空:            ← 0x473969，返回格子类型
#:                 sx += dx ; sy += dy
#:     return (sx, sy)
#:
#: 得到的是一个**指向实心那一侧**的矢量（没有归一化，也没有按距离加权 ——
#: 会话 25 那版「每格投一票单位矢量」猜错的正是这两点）。
#:
#: ★ 判据是「格子非空」（`test al,al; je`），**单向平台也算**（值 1）——
#: 所以这里用 `is_solid()` 而不是 `blocks_bullet()`。
TERRAIN_VOTE_WINDOW = 3

#: 反弹的两个系数，都是 `0x47c7a4: fld [0x69371c]` = **0.5**（V0.3 §110）：
#: 法向 `× −[vft+0x94]`、切向 `× (1 − [vft+0x98])`。两个都是 0.5 ⇒ 速度正好对折，
#: 和 §84 量到的「撞上之后 |v| 恰好减半」对得上。
BOUNCE_RESTITUTION = 0.5
BOUNCE_FRICTION = 0.5


def _terrain_facing(terrain, x, y):
    """`(x, y)` 那一片地形**朝哪边**：原版 `0x473b36` 的 7×7 投票（§110）。

    返回 `(sx, sy)`，**指向实心那一侧**（不是法线，法线是它的反向）；
    量不出朝向就返回 `None` —— 两种情况：一格实心的都没有（悬在空中），
    或者**整片都是实心**（采样点埋进地形里，49 票正负对消）。后者是采样点
    选错了的信号，调用方该换一个点再问一次。

    ★ 图顶上面（`y < 0`）和 `_probe_blocked()` 一个口径，不算实心（§83）。
    """
    cx, cy = int(round(x)), int(round(y))
    sx = sy = 0.0
    n = TERRAIN_VOTE_WINDOW
    for dy in range(-n, n + 1):
        yy = cy + dy
        if yy < 0:
            continue
        for dx in range(-n, n + 1):
            if not terrain.is_solid(cx + dx, yy):
                continue
            sx += dx
            sy += dy
    if abs(sx) < 1e-6 and abs(sy) < 1e-6:
        return None
    return (sx, sy)


def _reflect_velocity(vx, vy, facing):
    """把速度按地形朝向反射一次 —— 原版 `0x50f240` 那 12 句（§110）。

    ```
    θ  = atan2(sx, sy)              ← ★ 参数是 (x, y)，不是常见的 (y, x)
    u  =  cosθ·vx − sinθ·vy         ← 转进「以坡面为轴」的坐标系
    w  =  sinθ·vx + cosθ·vy
    u *= 1 − 摩擦(0.5)              ← 切向
    w *= −弹性(0.5)                 ← 法向取反
    转回来（角度 −θ）
    ```

    平地（`sx=0, sy>0` ⇒ `θ=0`）算出来正好是 `(0.5·vx, −0.5·vy)`；
    右侧竖直墙（`sy=0, sx>0` ⇒ `θ=π/2`）是 `(−0.5·vx, 0.5·vy)`
    —— §84 实测的 `v=(22.28, 12.50) → (−11.14, 6.25)` 一位不差。
    """
    theta = math.atan2(facing[0], facing[1])
    c, s = math.cos(theta), math.sin(theta)
    u = c * vx - s * vy
    w = s * vx + c * vy
    u *= 1.0 - BOUNCE_FRICTION
    w *= -BOUNCE_RESTITUTION
    # 逆旋转（cos(−θ)=c、sin(−θ)=−s）
    return (c * u + s * w, -s * u + c * w)


#: 找「挡住这一发的那一格」时最远往外看多少格。
#:
#: 圆心停在离面一个 `Size` 的地方（§83，抛射弹 8、直射弹 4），
#: 再宽一点兜住采样和取整的零头就够。
BLOCK_CELL_REACH = 14


def _nearest_solid(terrain, x, y, reach=BLOCK_CELL_REACH):
    """离 `(x, y)` 最近的那一格实心地形；`reach` 之内没有就返回 `None`。"""
    cx, cy = int(round(x)), int(round(y))
    best = None
    for oy in range(-reach, reach + 1):
        yy = cy + oy
        if yy < 0:
            continue
        for ox in range(-reach, reach + 1):
            if not terrain.is_solid(cx + ox, yy):
                continue
            span = ox * ox + oy * oy
            if best is None or span < best[0]:
                best = (span, cx + ox, yy)
    return None if best is None else (best[1], best[2])


def _block_facing(shell, terrain, ax, ay, bx, by, ground_t):
    """撞上的那一刻，地形朝哪边（§110）。量不出来返回 `None`。

    ★★ **采样点是挡住它的那一格地形，不是弹体圆心**（§110 末尾那张表）。
    原版 `0x50effb` 把命中结构的 `+4/+8` 先 `0x5f895c` 取整再交给
    `0x473b36` —— 那两格装的就是**格子坐标**。拿圆心去投票不行：
    `_terrain_contact()` 停下的地方离面还有一个 `Size`（§83），
    7×7 窗口根本够不着（55 次实测里 14 次一格实心都采不到）；
    拿这一步的**终点**也不行 —— 那个点常常整片埋在地形里，49 票正负对消。

    Iceria_b 那 55 次真实反弹（客户端逐帧日志）上，三种采样点的出射方向误差：

    | 采样点 | 中位 | p75 | p90 | 量不出 |
    |---|---|---|---|---|
    | 圆心（n=3）| —— | —— | —— | **55/55** |
    | 圆心 + `Size`·v̂（n=3）| 9.5° | 19.9° | 49.9° | 14 |
    | **挡住它的那一格（n=3）** | **1.2°** | **5.4°** | **9.5°** | **0** |

    而且窗口大小在 n=3 取到最好（n=2 → 3.1°，n=4 → 2.4°，n=6 → 4.4°）——
    和 `0x473b36` 里那两句 `cmp …, 3; jle` **独立地对上了**。

    ★★ **收口（V0.3 §116）**：现在拿得到**真正被挡住的那个采样点**了 ——
    采样点组是 `shell_probe_offsets()` 那两个，逐个查一下就知道是哪个撞上的，
    不用再拿「离接触点最近的实心格」去猜。实机对照（21 条客户端弹道）：
    句柄 200012 的末点误差 **24.16 → 0.01**、200193 **11.28 → 2.35**。
    `_nearest_solid()` 只留作兜底（采样点组和 `blocks_bullet` 口径对不上时）。
    """
    hx = int(ax + (bx - ax) * ground_t)
    hy = int(ay + (by - ay) * ground_t)
    radius = shell.radius
    if radius and radius >= 1.0:
        for ox, oy in shell_probe_offsets(radius, bx - ax, by - ay):
            if terrain.blocks_bullet(hx + ox, hy + oy):
                return _terrain_facing(terrain, hx + ox, hy + oy)
    elif terrain.blocks_bullet(hx, hy):
        return _terrain_facing(terrain, hx, hy)
    cell = _nearest_solid(terrain, float(hx), float(hy))
    if cell is None:
        cell = _nearest_solid(terrain, bx, by)
    if cell is None:
        return None
    return _terrain_facing(terrain, cell[0], cell[1])


def _bounce_shell(shell, terrain, ax, ay, bx, by, ground_t, free_t):
    """弹体撞地形之后**弹开**（§84 / §110）：夹回撞上之前那一点，再反射。

    夹回去这一步不能省：贴在地形里的话下一 tick 一开头又撞上，
    速度一路对折，弹体原地卡死（§76 里客户端那个「速度每帧对折、
    位置一动不动」就是这个样子）。

    ★★ **夹回去的那一点要取整**（V0.3 §116）：收方的扫掠是**逐整数格**走的
    （`0x50ea43` 的 `linePoint + offset`），挡住之后位置就是**最后一个通的
    那一格的整数坐标** —— 客户端逐帧日志里每一次反弹的落点都是整数
    （`(123, 835)` / `(277, 861)` / `(1245, 887)` …），一次例外都没有。
    留着小数会让下一 tick 的采样线整体偏半格，几十帧之后累出几十个单位。
    """
    px = float(int(ax + (bx - ax) * free_t))
    py = float(int(ay + (by - ay) * free_t))
    vx, vy = _shell_velocity(shell)
    facing = _block_facing(shell, terrain, ax, ay, bx, by, ground_t)
    if facing is None:
        # 一格实心都采不到（图外 / 数据缺）—— 只减半，方向不动。
        shell.vx, shell.vy = vx * BOUNCE_RESTITUTION, vy * BOUNCE_RESTITUTION
    else:
        shell.vx, shell.vy = _reflect_velocity(vx, vy, facing)
    shell.x, shell.y = px, py
    shell.bounced = True


def _resolve_terrain_block(shell, terrain, ax, ay, bx, by, ground_t, free_t):
    """撞地形之后按档位收口（§111）。**这一发还活着**就返回 `True`。

    2 档（`FlamingBottle` / `TrainingGrenade`）那道门抄的是 `0x47eec1`：
    拿 `0x473b36` 的 `(sx, sy)`，`2×|sx| ≤ sy` 就当场炸，否则弹开。
    平地上 `sx ≈ 0`、`sy > 0` ⇒ 炸；撞陡壁 / 天花板才弹。
    """
    mode = _blocked_mode(shell.weapon)
    if mode == BLOCKED_EXPLODE:
        return False
    if mode == BLOCKED_BOUNCE_IF_STEEP:
        facing = _block_facing(shell, terrain, ax, ay, bx, by, ground_t)
        if facing is None or 2.0 * abs(facing[0]) <= facing[1]:
            return False
    if mode == BLOCKED_STICK:
        # `0x47ee6d`：速度清零、位置夹回接触点。下一 tick 重力又把它按回
        # 地形里，再清一次 —— 效果就是钉在那儿等引信 / 寿命。
        shell.x = ax + (bx - ax) * free_t
        shell.y = ay + (by - ay) * free_t
        shell.vx = shell.vy = 0.0
        shell.bounced = True
        return True
    _bounce_shell(shell, terrain, ax, ay, bx, by, ground_t, free_t)
    return True


def _reflecting_seats(room, now):
    """此刻**开着反射护盾**的那些座位（V0.3 §119）。到点的顺手摘掉。"""
    quest = getattr(room, "quest", None) if room is not None else None
    table = getattr(quest, "reflect_until", None)
    if not table:
        return ()
    live = []
    for seat, until in list(table.items()):
        if until <= now:
            del table[seat]
        else:
            live.append(seat)
    return tuple(live)


def _reflect_shield_hit(room, shell, ax, ay, bx, by, bodies):
    """这一步有没有撞进谁的反射护盾。撞了返回 `(t, 圆心x, 圆心y, 座位)`。

    ★ 护盾的圆心用**身体那个圆**的圆心（`chrprops.center()`）——
    原版取的是 `[角色 vft+0x74]`，和瞄准用的那个点同源（§62）。
    """
    if not bodies:
        return None
    seats = _reflecting_seats(room, _now())
    if not seats:
        return None
    reach = gameserver.REFLECT_RADIUS + shell.radius
    best = None
    for seat_index, px, py, crouched, character_id in bodies:
        if seat_index not in seats:
            continue
        character = chrprops.get(character_id)
        cx, cy = character.center(px, py, crouched)
        t = _segment_circle_t(ax, ay, bx, by, cx, cy, reach)
        if t is None or (best is not None and t >= best[0]):
            continue
        best = (t, cx, cy, seat_index)
    return best


def _reflect_off_shield(shell, ax, ay, bx, by, shield):
    """弹体被反射护盾弹开（V0.3 §119）—— 原版 `0x47f0b4` 起那一段。

    原版是「沿 −v 一步一步退到圆外（最多 5 步），再解线与圆的交点、
    对半径方向镜像速度」。退出来那一步和这里的「夹到交点上」是同一件事
    （交点本来就在圆上），所以直接用交点。

    ★ **速度只换方向、不减半**：这条路上没有地形反弹那两个 `× 0.5`
    （`vft+0x94` / `+0x98`，§110）—— 护盾是把弹体原样弹回去。
    """
    t, cx, cy, _seat = shield
    px = ax + (bx - ax) * t
    py = ay + (by - ay) * t
    vx, vy = _shell_velocity(shell)
    nx, ny = px - cx, py - cy
    span = math.hypot(nx, ny)
    if span < 1e-6:
        shell.vx, shell.vy = -vx, -vy
    else:
        nx /= span
        ny /= span
        dot = vx * nx + vy * ny
        shell.vx = vx - 2.0 * dot * nx
        shell.vy = vy - 2.0 * dot * ny
    shell.x, shell.y = px, py
    # 弹过之后闭式解不作数了，和撞地形那一路一样改走逐 tick 积分。
    shell.bounced = True


def _mob_contact(room, shell, ax, ay, bx, by):
    """这一 tick 有没有扫中一只怪：`(t, 句柄)`；没有返回 `None`。

    位置来自控制者广播的 `rpAiMsg`（§125），句柄和 `rpExplode +4` 是同一套
    ⇒ 填进爆炸包收方就照着扣血。
    ⚠ 半径统一按 `MOB_HIT_RADIUS`（`Mob.ini` 的众数 40），因为服务端没法把
    句柄映射回怪的类型。这一格实机最该盯（见那个常量的注释）。
    """
    best = None
    for mx_, my_, handle in live_mobs(room):
        if not handle:
            continue
        t = _segment_circle_t(ax, ay, bx, by, mx_, my_,
                              MOB_HIT_RADIUS + shell.radius)
        if t is None or (best is not None and t >= best[0]):
            continue
        best = (t, handle)
    return best


def _shell_step(room, shell, terrain, bodies):
    """把这颗子弹往前推**一个 tick**，返回 `(落点, 座位号或None, 部位或None)`。

    什么都没撞上返回 `None`（子弹继续飞，`shell.x/y` 已经更新）。
    同一段里既撞地形又撞人时，**参数 `t` 小的那个先发生**。

    ★★ 带引信的弹体撞地形**不结算**，弹开接着飞（§84）——
    那一路也返回 `None`。
    """
    ax, ay = shell.x, shell.y
    if shell.weapon.homing_angle:
        bx, by = _homing_step(room, shell, bodies)
    elif shell.bounced:
        # 弹过之后闭式解不作数了 —— 和追踪弹一样逐 tick 积分。
        shell.vy += shell.shot.gravity
        bx, by = shell.x + shell.vx, shell.y + shell.vy
    else:
        bx, by = shell.position(shell.ticks + 1)
    shell.ticks += 1
    # ★★★ 反射护盾排在最前面（§119）：它那个圆半径 50，比谁的碰撞圆都大，
    #     所以只要有人开着护盾，弹体一定先碰到圆再碰到人。
    shield = _reflect_shield_hit(room, shell, ax, ay, bx, by, bodies)
    if shield is not None:
        _reflect_off_shield(shell, ax, ay, bx, by, shield)
        return None
    best_t = None
    best = None
    radius = shell.radius
    for seat_index, px, py, crouched, character_id in bodies:
        character = chrprops.get(character_id)
        for cx, cy, r, region in character.circles(px, py, crouched):
            t = _segment_circle_t(ax, ay, bx, by, cx, cy, r + radius)
            if t is None or (best_t is not None and t >= best_t):
                continue
            best_t = t
            best = (seat_index, region)
    # ★★ 闯关房：也要撞得到怪（M5-G）。排在地形前面 —— 怪站在地上，
    #    先撞怪再撞地才是对的。
    mob = _mob_contact(room, shell, ax, ay, bx, by)
    if mob is not None and (best_t is None or mob[0] < best_t):
        best_t = mob[0]
        best = (("mob", mob[1]), None)
    ground_t, free_t = _terrain_contact(terrain, ax, ay, bx, by, radius)
    if ground_t is not None and (best_t is None or ground_t < best_t):
        if _resolve_terrain_block(shell, terrain, ax, ay, bx, by,
                                  ground_t, free_t):
            return None
        best_t = ground_t
        best = (None, None)
    shell.x, shell.y = bx, by
    if best is None:
        _jump_pad_shell(shell, terrain)
        return None
    point = (ax + (bx - ax) * best_t, ay + (by - ay) * best_t)
    shell.x, shell.y = point
    return (point, best[0], best[1])


def _jump_pad_shell(shell, terrain):
    """弹跳台把**弹体**也弹起来（V0.3 §103）。弹了返回 `True`。

    ## 这是原版行为，不是猜的

    用户 2026-08-29：「手雷会在弹跳台上弹跳，这是这个游戏的特色；在垂直的
    弹跳台上不停扔手雷可以封路。」—— `JumpingObj::Tick`（`0x510d05`）里
    确实有**第二条分支**（`0x510fb4`），对同一个对象再做两次 `dynamic_cast`：

        0x510fb9  目标 0x6e06ac = .?AVMobObject@@     ← 怪
        0x510fcb  目标 0x6dff90 = .?AVBulletObj@@     ← ★ **弹体**
        两个有一个成就继续；再 0x50f410 判碰撞

    弹体这一档和角色那一档共用同一个解算器 `0x5111ca`，唯一的差别是
    **没有** `− 0.25 × 重力` 那一项（`0x510e68` 只在角色分支里）：

        tx = 台dx + (台x − 弹x)
        ty = 台dy + (台y − 弹y)
        vy = −sqrt(2 × g × |ty|) ; vx = tx / (|vy| / g)

    ## 实机逐帧对死了

    2026-08-29 那一局 `Iceria_b`，真人的手雷句柄 **100048**：
    第 5 帧还是 `v = (22.66, 33.34)`（往右下砸），第 6 帧位置
    `(771.30, 877.91)`、速度**突变成 `(2.43, −30.43)`**（直冲上天）。
    把第 6 帧的位置代进上面三行 ⇒ `(2.43, −30.43)`，**一位不差**。
    句柄 100058 同样对上。

    ## 判定是**扫掠**的，不是「此刻在不在圈里」

    `0x50f410` 把两边的**速度**（`[obj+0x120]`）一起传给 `0x50c758`，
    做的是「这一步扫过去的那一段」相交。句柄 100048 触发那一刻，弹体圆心
    离台心还有 **36.5**（比 20 + 8 大），但它这一步要扫到 `(793.96, 912.21)`
    —— 那条线离台心只有 **0.7**。不按扫掠算的话这一发根本判不出来。

    ## ★★★ 只有「撞地形不当场炸」的那几类会被弹（§112，用户 2026-08-29 报的）

    静态分析看不出过滤（19 个弹体类的 RTTI 基类链全含 `BulletObj`，
    `0x50f410` 那道 `test [shape+0xc], mask` 传的 mask 恒 0），
    **但客户端逐帧日志说得很清楚**（`Iceria_b`，两台的 `PROJ.` 探针）：

    | 弹体类 | 扫过台子（半径 20+Size）几次 | 被弹起几次 |
    |---|---|---|
    | `BulletObj`（`ch00-01` / `ch00-02a` / **`ch02-02`**）| **17** | **0** |
    | `AppleGrenade`（`ch00-02`）| 多次 | 每次都弹 |

    `ch02-02` 那几发（13:55:34 / 13:55:38）**扫到离台心 10~17 就到此为止**
    —— 客户端把它当地形撞掉了；13:55:40 那一发干脆从台子正上方穿过去、
    弹道一点没变。用户报的正是这个：「炮弹飞到弹跳台后就消失了，
    过一会儿在弹跳台弹跳后的上方位置出现爆炸动画」—— 消失的是客户端那颗
    （撞地形炸了），后来那个爆炸是服务端这颗被台子弹上天之后才炸的。

    ⇒ 服务端按同一条线收口：**`_bounces_off_terrain()` 为假的一律不弹**。
    （机制上最可能的原因是这类弹体压根没往 `[obj+0x140]` 挂碰撞形状，
    于是 `0x50f410` 的内层循环一次都不跑 —— 那一步还没证实，
    但「弹不弹」这条线是实测出来的。）
    """
    pads = getattr(terrain, "jump_pads", ()) if terrain is not None else ()
    if not pads:
        return False
    if not _bounces_off_terrain(shell.weapon):
        return False
    vx, vy = _shell_velocity(shell)
    reach = botmove.JUMP_PAD_RADIUS + shell.radius
    for px, py, dx, dy in pads:
        if _segment_circle_t(shell.x, shell.y, shell.x + vx, shell.y + vy,
                             px, py, reach) is None:
            continue
        ty = dy + (py - shell.y)
        if ty >= 0.0:
            continue
        launch_y = -math.sqrt(2.0 * botmove.GRAVITY * abs(ty))
        ticks = abs(launch_y) / botmove.GRAVITY
        launch_x = (dx + (px - shell.x)) / ticks if ticks else 0.0
        shell.vx, shell.vy = launch_x, launch_y
        # ★ 弹过之后闭式解不作数了 —— 和撞地形弹开走同一条积分路（§84）。
        shell.bounced = True
        return True
    return False


#: ★★ **角色在溅射判定里的半径**：所有角色都是 **35**，写死在
#: `Character` 虚表槽 `+0x7c`（`0x4fc229: fld [0x693758]`）。
#:
#: 它被加进溅射的**作用半径**里 —— 也就是说 `SplashRange` 量的是「爆点到
#: 身体表面」，不是「爆点到身体中心」。
SPLASH_BODY_RADIUS = 35.0


def mob_bodies(room):
    """场上的怪，摆成和 `_battle_bodies()` 一样的五元组（V0.3 §129）。

    `(("mob", 句柄), x, y, False, None)` —— 第一格用元组，和
    `_resolve_shell()` 里那套 `("mob", 句柄)` 是同一个约定；
    `character_id` 填 `None` = 「没有三个碰撞圆，按 `MOB_HIT_RADIUS` 算」。
    """
    return [(("mob", handle), float(mx), float(my), False, None)
            for mx, my, handle in live_mobs(room) if handle]


def _splash_targets(room, shell, point, victim_seat, bodies, victim_mob=None):
    """爆炸溅到了谁：`[(座位号, 伤害, 受击点, 击退矢量)]`。没有溅射就是空表。

    ## ★★★ 衰减公式是**逆出来的**，不是猜的（§90）

    `SplashDamage` 虚表槽 `+0x134`（`0x4857aa`）算的就是这一档：

        0047e6..  r = |爆点 → 目标身体圆心| / (SplashRange + 35)
        004857f0  fdivr / fcom 1.0 -> r > 1 就是**一点伤害都没有**
        00485871  call 0x5ce3a0(r, 1.0)   ; = pow(r, 1/1.0) = r
        00485886  (1 − r) × (SplashDamage − 1)
        0048588b  + 1
        00485891  call 0x5f895c           ; ★ 朝零截断

    ⇒ `伤害 = int((1 − r) × (SplashDamage − 1) + 1)`，**边缘上是 1 不是 0**。
    ×2（夺分）在这之后才乘（`0x480e52` 那一发 `[vft+0x128]` = `0x4806bf`）。

    语料实证：只看**自伤**（受害者就是射手，位置有他自己的心跳）——
    `ch02-03` 5/5、`ch105-02` 3/3、`ch101-02` 14 发里 10 发一位不差；
    旧的「`round(SplashDamage × 倍率 × (1 − 距离/SplashRange))`」在同一批
    样本上**系统性偏低**（合计误差 268 vs 32）。

    ★★★ `bodies` 必须是**不按碰撞组过滤**的那一份（`_battle_bodies` 的
    `group=None`）：溅射**分不清敌我**，队友和射手自己都吃（§69）。
    过滤了的话组队房里 bot 的手雷炸在队友脚下一滴血都不掉，
    和原版对不上。
    ★ **闯关房例外**：那儿 `_battle_bodies()` 一个角色都不返回（§142），
    于是这张名单里只剩怪 —— 任务模式没有任何角色间伤害。

    ★ 直接命中的那个人**不重复算**（他已经吃过 `rpExplode` 那一档伤害了）。

    ⚠ 这里**没有**夺分那两条 ×0.75（§89）—— 它们在 `0x47e618` 里，
    只有直接命中那条路会过。

    ★★ **怪也吃溅射**（V0.3 §134）：`bodies` 里 `character_id is None` 的那几行
    就是怪（`mob_bodies()`），第一格是 `("mob", 句柄)`。原版的溅射对象组恒为 0
    = 撞所有人，怪自然也在里面 —— 服务端以前只扫座位，于是闯关模式里 bot
    的手雷炸在一堆怪中间**一滴血都不掉**（用户 2026-08-30）。
    `victim_mob` 是被**直接**命中的那只，不重复算。
    """
    weapon = shell.weapon
    span_max = float(weapon.splash_range or 0.0)
    if span_max <= 0.0:
        return []
    # ★ 目标那 35 是加在**半径**上的（`0x485831` 把两边的 `vft+0x7c` 相加）。
    reach = span_max + SPLASH_BODY_RADIUS
    # ★ 强力射击的 `DamageRatio` 也吃在溅射上（§117）。
    full = int(weapon.splash_damage * shell.damage_ratio)
    scale = _damage_scale(room)
    out = []
    for seat_index, px, py, crouched, character_id in bodies:
        if character_id is None:
            # 怪：句柄映射不回 `Mob.ini` 的类型 ⇒ 没有三个碰撞圆，
            # AI 广播的那个点就当身体圆心（和 `_mob_contact()` 同口径）。
            if victim_mob is not None and seat_index[1] == victim_mob:
                continue
            cx, cy = px, py
        else:
            if seat_index == victim_seat:
                continue
            character = chrprops.get(character_id)
            cx, cy = character.center(px, py, crouched)
        span = math.hypot(cx - point[0], cy - point[1])
        if span > reach:
            continue
        # ★ 朝零截断（`0x5f895c` 是 `_ftol2`，等价于 C 的 `(int)`），
        #   **然后**才乘模式倍率 —— 顺序和原版一致（§90）。
        base = int((1.0 - span / reach) * (full - 1) + 1)
        damage = base * scale
        if damage <= 0:
            continue
        # ★★ 击退（§92）：来向是**爆点指向身体圆心**那一条，强度按**没乘
        #   模式倍率**的那个伤害查阶梯（`0x4858a2` 传的就是 `+0x134` 的返回
        #   值，`×2` 是后面 `+0x128` 才乘的）。
        push = knockback_vector(cx - point[0], cy - point[1], base)
        out.append((seat_index, damage, (cx, cy), push))
    return out


def _shell_fell_out_of_the_world(room, shell, point):
    """这颗弹体是**掉出地图下边界**没的吗（V0.3 §161）。

    ★★★ 判据和角色那条（`_fell_out_of_the_world` / §143）同一套：
    这张图的 `map.ini` 有 `FallDown`，而**拦住它的那个采样点**已经在图外。

    ★ 为什么要把碰撞半径加回去：`_terrain_contact()` 是拿弹体外缘的探针
      去问地形的（`shell_probe_offsets`，往下飞时偏移就是 `+radius`），
      所以「探针出界」这件事在落点上写着的是 `y + radius >= 图高`。
      直接拿 `y` 比会漏掉一切带半径的弹体（半径 8 的能差 8 个像素）。

    ## 为什么要单独认出这一种

    实机对账（2026-09-01 23:14，`Esperan00`）：苹果雷的第 4 片碎片
    （客户端句柄 400024）第 42 帧还在 `(955, 2022)`、竖直速度 29.4，
    下一帧就越过图高 2048 —— 客户端**把它删掉了，一个爆炸对象都没建**
    （另外三片各自在落地那一帧后 32 ms 建出了 400027/400028/400029）。
    而服务端照旧把它当「撞到实心」结算，`explode(spawns=1)` 替那个
    根本不存在的对象记了一个句柄 ⇒ 从这一发起，这个座位的句柄
    **永久错开 1**，之后每一发 `rpExplode` 都被 `0x492750` 静默丢弃
    （§42）：子弹照飞、爆炸动画没有、一滴血不掉。

    对账证据（客户端 `PROJ+` 的句柄 vs 服务端 `rpFire` 预测的句柄）：

    | 局 | 地图 | `FallDown` | 五个 bot 的最终偏差 |
    |---|---|---|---|
    | 22:53 | `Iceria00` | 否 | 0 0 0 0 0 |
    | 22:58 | `Esperan00` | **是** | 13 6 20 0 0 |
    | 23:03 | `Esperan00` | **是** | 19 41 101 0 0 |
    | 23:08 | `Forest00` | 否 | 0 0 0 0 0 |
    | 23:13 | `Esperan00` | **是** | 188 138 125 196 142 |

    用户 2026-09-01 报的「bot3 打我没有伤害」「苹果弹没有爆炸动画也没有
    伤害」「出问题的几局好像都是有岩浆的地图」—— 岩浆图就是 `FallDown` 图。
    """
    if room is None:
        return False
    if not mapdata.falls_out_of_the_world(getattr(room, "map_name", "")):
        return False
    terrain = _terrain(room)
    if terrain is None:
        return False
    radius = float(getattr(shell, "radius", 0.0) or 0.0)
    return float(point[1]) + radius >= float(terrain.height)


def _resolve_shell(room, machine, shell, point, victim_seat, region, tick):
    """这颗子弹到头了：发 `rpExplode`（+ 溅射的 `rpSplashDamaged`）。

    `victim_seat is None` = 打在地形上 / 飞出图外 —— 照样要发，
    **句柄记账不许漏**（§42）。

    ★★★ 唯一的例外是**掉出下边界**（§161）：收方把那种弹体静默删掉、
    不建爆炸对象，这边跟着一句都不发、一个句柄都不记，两边才对得上。
    """
    if _shell_fell_out_of_the_world(room, shell, point):
        # 收方那边这颗弹体已经没了：发了也是被静默丢弃，而 `spawns` 那一个
        # 句柄它**根本不会记** —— 记了就是永久错开（§42）。
        # ★ 分裂 / 火墙 / 破坏物那几段也一并跳过：删掉的弹体跑不出 `Tick`，
        #   自然一片碎片都不生（§81 的分裂就挂在 `AppleGrenade::Tick` 上）。
        return
    if machine.no_explode:
        # ★ 诊断开关（`/noboom`）：到头了也不发爆炸，让弹体一直飞下去。
        #   记录照样出队 —— 收方没收到爆炸就不会创建溅射对象，
        #   这边跟着一个都不记（§86），两边天然一致。
        return
    weapon = shell.weapon
    # ★★ 打中的是**一只怪**时 `victim_seat` 是 `("mob", 句柄)`（M5-G）：
    #    目标句柄直接填它的，伤害不走「按部位分档 / 夺分 ×2」那套 ——
    #    那两条都是打**角色**才有的（`0x47e618` 的门）。
    mob_handle = (victim_seat[1]
                  if isinstance(victim_seat, tuple) else None)
    if mob_handle is not None:
        victim_seat = None
    hit = victim_seat is not None
    # ★★ 夺分模式伤害翻倍（§87）+ 夺分独有的两条 ×0.75（§89）——
    #   原版是射手那台机器在把数字塞进包之前做的
    #   （`0x4806f1: shl` / `0x47e6df` / `0x47e6fe`）。
    damage = (_direct_hit_damage(room, machine, weapon, region, victim_seat,
                                 shell.damage_ratio)
              if hit else 0)
    if mob_handle is not None:
        damage = int(weapon.damage_for("body") * shell.damage_ratio)
        # ★ 闯关分数（§130）：打在怪身上的伤害就是分。
        _score_quest_damage(room, machine, damage)
    # ★★★ 组包 + **爆炸对象的句柄记账**在同一次加锁里（§86）：收方处理这一
    #   发时会创建那个溅射对象，它和弹体共用同一个计数器。
    packet = machine.sync.explode(
        shell.handle,
        mob_handle if mob_handle is not None
        else (botsync.character_handle(victim_seat) if hit else 0),
        point[0], point[1],
        hit_kind=(botsync.HIT_CHARACTER
                  if (hit or mob_handle is not None) else botsync.HIT_NONE),
        damage=damage, spawns=weapon.explode_step)
    # ★ 诊断：命中 / 落空**各打一行**（按状态翻转去重，铁律 10）。M3b 收口后删。
    if (hit, mob_handle is not None) not in machine.explode_logged:
        machine.explode_logged.add((hit, mob_handle is not None))
        head, body = packet[:12], packet[12:]
        machine.log(f"   爆炸: 弹体句柄 {shell.handle} "
                    f"目标 {'座位%d 的%s' % (victim_seat, region) if hit else ('怪 %d' % mob_handle) if mob_handle is not None else '落空'} "
                    f"爆炸点 ({point[0]:.1f}, {point[1]:.1f}) 伤害 {damage}"
                    f"　飞了 {shell.ticks} tick"
                    f"；头 {head.hex()} body({len(body)}) {body.hex()}")
    _emit(machine, packet)
    # ★★ 直接命中的击退**不进包**：每台机器都拿它自己那颗弹体的速度现算
    #   （`0x49285c` 读 `[proj+0x120]`，§92）。所以只有「被打的是另一个
    #   bot」时服务端才要自己补一份 —— 真人那份归他自己那台机器。
    if hit:
        _note_damage(room, victim_seat, damage)     # ★ 血量台账（M5-C）
        vx, vy = _shell_velocity(shell)
        _knock_back_seat(room, victim_seat, damage,
                         knockback_vector(vx, vy, damage),
                         source="bot 直接命中")
    # ★★★ 溅射的名单和弹体**不是同一份**（§69）：弹体按碰撞排除组过滤
    #   （队友撞不着），而溅射对象的组恒为 0 = **撞所有人** ——
    #   队友、连射手自己都吃。所以这里重新问一次、不带组。
    #   ★★ 名单里还要有**怪**（§134）：溅射对象撞所有人，怪自然也在里面。
    #   ★★★ 闯关房里角色那半截是空的（§142）—— 任务模式只伤怪和场景物。
    for seat_index, splash, where, push in _splash_targets(
            room, shell, point, victim_seat,
            _battle_bodies(room, machine.my_seat, include_self=True)
            + mob_bodies(room),
            victim_mob=mob_handle):
        # ★ 溅射伤害得**单独报**：收方处理 `rpExplode` 时确实会建一个
        #   `SplashDamage` 对象（§54 那个多出来的句柄），但算伤害的是射手
        #   那台机器 —— bot 没有本机，不补这一发就一滴血都不掉（§67）。
        # ★★ `+13/+17` 是**击退矢量**（§92）：不填的话被溅到的人一动不动，
        #   而真人扔的同一颗手雷会把人顶飞 —— 用户 2026-08-28 报的就是这个。
        splashed_mob = (seat_index[1] if isinstance(seat_index, tuple)
                        else None)
        _emit(machine, machine.sync.event(
            botsync.OP_SPLASH_DAMAGED,
            botsync.splash_body(shell.handle,
                                splashed_mob if splashed_mob is not None
                                else botsync.character_handle(seat_index),
                                splash, where[0], where[1],
                                push_x=push[0], push_y=push[1])))
        if splashed_mob is not None:
            # 怪没有血量台账，击退也归控制者那台算 —— 这边只记分（§130）。
            _score_quest_damage(room, machine, splash)
            continue
        _note_damage(room, seat_index, splash)       # ★ 血量台账（M5-C）
        _knock_back_seat(room, seat_index, splash, push, source="bot 溅射")
    # ★★★ **直接砸中人的那一发不铺火墙**（§79）—— 铺火那一段前面有一道
    #   `cmp dword [esp+8], 0 ; jne 出口`（`0x4829d7`），`[esp+8]` 就是
    #   「撞上的那个角色」的指针（基类 `0x47eb67` 拿它和 0 比），非空 ⇒
    #   整段跳过。语料 1079 发 `rpSetOnFire` **无一例外**跟在「什么都没打中」
    #   的那一发爆炸后面；反过来 308 发「打中角色」的火焰弹爆炸里只有 1 发
    #   后面跟着火墙（那一发是同一台机器上另一颗弹体的）。
    #
    #   用户 2026-08-28：「我自己扔出去后如果直接命中别人，那么别人是一次性
    #   收到伤害，之后就不会再有持续伤害了……而 bot 扔出去的手雷，即便是
    #   直接命中我，我身上的火焰也会持续造成伤害。」
    if not hit:
        _set_ground_on_fire(room, machine, weapon, point,
                            _tick_moment(shell, tick))
    # ★★★ **冰块 / 木箱也吃这一发**（§139）：破坏物和角色走的是同一条
    #   伤害路（`0x480dfb`），只是命中判据和半径不一样。
    _blast_breakables(room, machine, weapon, shell, point)
    _split_shell(room, machine, shell, point, victim_seat, tick)


def _blast_breakables(room, machine, weapon, shell, point):
    """一发爆炸落在 `point`：照原版判哪几件可破坏物挨到了（§139）。

    ## ★★★ 为什么非得**补发** `rpSplashDamaged`

    别人机器上跑不出这一下：那条「炸到破坏物没有」的遍历
    （`DamagingObj +0x11c` = `0x480469`）外面套着一道
    `0x50d294` =「这颗弹是我的 / 中立的吗」。bot 的弹在真人那台机器上
    两样都不是 ⇒ 整段跳过。所以**射手那台**（这里就是服务端）不补发，
    真人屏幕上的冰就永远不碎，和服务端的模型对不上。

    真人自己打的那一下不走这里 —— 他的客户端已经广播过了，
    服务端在 `note_peer_hit()` 里照着扣就行。
    """
    ledger = _breakables(room)
    if ledger is None:
        return ()
    terrain = mapdata.load(_current_map(room))
    if terrain is None or not terrain.breakables:
        return ()
    splash_range = float(getattr(weapon, "splash_range", 0.0) or 0.0)
    splash_damage = float(getattr(weapon, "splash_damage", 0.0) or 0.0)
    hits = ledger.blast(terrain, point[0], point[1], splash_range,
                        splash_damage, mult=_damage_scale(room),
                        now=None)
    for item, hurt, where, broke in hits:
        # ★ 和打人那一发用同一个组包（`botsync.splash_body`）——
        #   区别只是 `+4` 填的是**世界句柄**而不是角色句柄。
        _emit(machine, machine.sync.event(
            botsync.OP_SPLASH_DAMAGED,
            botsync.splash_body(shell.handle, item.handle, hurt,
                                where[0], where[1])))
        if broke:
            machine.log(f"   破坏物碎了: 句柄 {item.handle} @ "
                        f"({item.x}, {item.y})　"
                        f"{item.regen_ms / 1000.0:.0f} 秒后长回来")
    return hits


def _fire_wall_of(weapon):
    """这把武器炸完会不会在地上铺一道火墙；会的话返回那一节火焰的武器记录。

    只有 `CreatingClass=FlamingBottle` 会（`0x4829b1` 那个 `IsMine` 门后面
    那一发 `rpSetOnFire`，§75）。别的武器返回 `None`。
    """
    if weapon.raw.get("creating_class") != "FlamingBottle":
        return None
    slice_id = weapon.raw.get("slice_id")
    return None if not slice_id else weapondata.get(slice_id)


#: ★★★ 同一个人两次挨烧之间至少隔多少个 tick —— **20**，原版硬写在
#: `Flame` 的构造里（`0x485ab9: mov dword ptr [esi+0x300], 0x14`，§85）。
#:
#: 这一格是传给「这个源现在能不能伤我」那道门的**免伤时长**：
#: `Flame` 的 `[vft+0x140]`（`0x485e7a`）把它连同自己一起交给**受害者**的
#: `[vft+0xcc]`，最后落到 `0x50f7a7`：
#:
#:     add ecx, 0x160        ; ★ 时刻戳记在**角色身上**，一个人只有一格
#:     call 0x409fdd         ; now（逻辑 tick 计数器，不是毫秒）
#:     cmp eax, [ecx] ; jl   ; 还没到点 -> 这一发不算
#:     call 0x409fdd
#:     add eax, [esp+8]      ; now + 20
#:     mov [ecx], eax
#:
#: ★ 语料独立对上过同一个数（§78）：380 份语料里的 102 次火烧，同一个
#:   受害者两次之间**恒隔 5 发心跳**（5 × 128 ms = 640 ms = 20 tick），
#:   三种参数完全不同的火焰（`ch01-02a` / `ch100-02a` / `ch103-02a`）
#:   给出同一个数 —— 那时候只知道「和武器参数无关」，现在知道为什么了。
BOT_FIRE_REBURN_TICKS = 20


#: 一帧最多替火墙补推多少个 tick。★ 不是超时阈值，是**掉帧的兜底**：
#: 服务端帧跟着真人心跳走，卡一下不该让火墙一口气把欠的都烧回来。
#: 取最长的那种火墙的寿命（`ch01-02a`：60 + 4×4 = 76）再留一点余量。
BOT_FIRE_CATCHUP_TICKS = 128


def _clock_tick(now):
    """把 `time.monotonic()` 的秒数换成**绝对** tick 号（32 ms 一个）。

    只用来做「隔了多少个 tick」的差值，原点在哪无所谓。
    """
    return int(now * ballistics.TICKS_PER_SECOND)


class Flame(object):
    """火墙里的**一团**火（§79）。

    ★ 它是收方一个真的 `Flame` 对象（`0x492426` 造的那个类），有自己的
    弹体句柄、自己的出生时刻、自己的寿命。
    """

    __slots__ = ("handle", "x", "y", "born", "dies")

    def __init__(self, handle, x, y, born, dies):
        self.handle = int(handle)
        self.x = float(x)
        self.y = float(y)
        #: 第几个 tick 生出来（根火 = 0，往外一格加一个 `SpawnInterval`）。
        self.born = int(born)
        #: 第几个 tick 消失（`born + SpawnLifeTime`）。
        self.dies = int(dies)

    def alive(self, tick):
        return self.born <= tick < self.dies

    def __repr__(self):
        return ("<Flame %d (%.0f, %.0f) %d~%dtick>"
                % (self.handle, self.x, self.y, self.born, self.dies))


class FireWall(object):
    """地上那一道**还在烧**的火（§75 / §78 / §79）—— 服务端这边的那一份。

    收方那边是 **`2 × SpawnCount + 1` 个 `Flame` 对象**，不是一次性造好的：
    `OnSetOnFire`（`0x492471`）只造**根火**那一团，之后每一团在自己出生
    `SpawnInterval` 个 tick 之后再往外生一团（`Flame::Tick` `0x482696`
    那一段），根火**左右各生一路**、子火只往自己那个方向传
    （`0x4827de: add [ebp-0x14], 2` 那个 −1 / +1 的循环）。
    所以整道墙是**从爆点往两边铺开**的，宽度 `± SpawnCount × SpawnDistance`。

    ★ 这正好解释了 §75 那个句柄数 `2n+1`：根火 1 个 + 左右各 n 个。

    但**算伤害的还是射手那台机器**（和弹体、溅射同一条守卫，§42），
    bot 没有本机 ⇒ 不补 `rpSplashDamaged` 的话，火看得见、站上去不掉血。
    """

    __slots__ = ("handle", "flame", "flames", "born", "born_tick", "ticks",
                 "max_ticks", "burnt")

    def __init__(self, handle, flame, flames, born, max_ticks):
        #: 这道墙那一段句柄的**头一个**（= 根火）。
        self.handle = int(handle)
        self.flame = flame
        #: 每一团火（`Flame`），按收方的创建顺序：根、左1、右1、左2、右2……
        self.flames = list(flames)
        self.born = float(born)
        #: ★ 点着的那一刻是**绝对**第几个 tick —— 几道墙共用一条时间轴，
        #:   「谁上一次被烧是什么时候」才记得到一起去（§85）。
        self.born_tick = _clock_tick(born)
        self.ticks = 0
        self.max_ticks = int(max_ticks)
        #: ★ 留着只为日志和单测好看；**再烧间隔的账不在这儿**，
        #:   在 `BotConn.burnt` 上（§85）。
        self.burnt = {}

    @property
    def spots(self):
        """所有火团的落点（不分死活）—— 日志和单测看的就是它。"""
        return [(f.x, f.y) for f in self.flames]

    def __repr__(self):
        return ("<FireWall %d %d团 %d/%dtick>"
                % (self.handle, len(self.flames), self.ticks, self.max_ticks))


def _flame_ground(terrain, x, y, reach):
    """火团往 `x` 那一列挪一格之后落在哪；落不住返回 `None`（§79）。

    收方是拿**通用的横向移动**把子火从父火那儿挪出去的
    （`0x50d9a7`，和人走路是同一个例程），挪不动 / 落点在地形里就当场
    把这团火销毁（`0x4827b8` / `0x4827c9` 那两支都接着 `[vft+0x20]`）。
    ⇒ 服务端这边用**同一个**横向移动模型：`botmove.surface_near()`
    在这一列上找离父火最近的站立面，找不到就是「这一路到此为止」。
    """
    if terrain is None:
        return y
    surface = botmove.surface_near(terrain, x, y, reach)
    return None if surface is None else float(surface)


def _fire_wall_flames(terrain, flame, point, handle):
    """摆好一道火墙的 `2n+1` 团火（§79）。

    * 根火在**爆点**上（`0x492481` 把 y 减了 1：`fsub [0x693720]` = 1.0）；
    * 之后每 `SpawnInterval` 个 tick 往外铺一格，一格 `SpawnDistance`；
    * 左右两路各铺 `SpawnCount` 格，某一路落不住就那一路停下
      （剩下的句柄照样占着 —— 收方是**开头一次性**分配 `2n+1` 个的）。
    """
    count = int(flame.raw.get("spawn_count") or 4)
    gap = float(flame.raw.get("spawn_distance") or 30.0)
    interval = int(flame.raw.get("spawn_interval") or 4)
    life = int(flame.raw.get("spawn_life") or 60)
    reach = gap * botmove.CLIMB_SLOPE
    root_y = point[1] - 1.0
    flames = [Flame(handle, point[0], root_y, 0, life)]
    # ★ 句柄按**收方的创建顺序**发：根、（第 1 个 interval）左1 右1、
    #   （第 2 个）左2 右2……—— 根火先造，之后每一 tick 两路各生一团。
    tails = {-1: (point[0], root_y), 1: (point[0], root_y)}
    next_handle = handle + 1
    for step in range(1, count + 1):
        for direction in (-1, 1):
            tail = tails.get(direction)
            this_handle, next_handle = next_handle, next_handle + 1
            if tail is None:
                continue                      # 这一路已经断了
            x = tail[0] + direction * gap
            y = _flame_ground(terrain, x, tail[1], reach)
            if y is None:
                tails[direction] = None       # 挪不过去 —— 这一路到此为止
                continue
            tails[direction] = (x, y)
            flames.append(Flame(this_handle, x, y,
                                step * interval, step * interval + life))
    return flames


def _fire_wall_ticks(flame):
    """这道火墙一共活几个 tick。

    最后一团火在第 `SpawnCount × SpawnInterval` 个 tick 上生出来，
    再活 `SpawnLifeTime` 个 tick（三个数都是 `weapon.ini` 的）。
    """
    life = int(flame.raw.get("spawn_life") or 60)
    count = int(flame.raw.get("spawn_count") or 4)
    interval = int(flame.raw.get("spawn_interval") or 4)
    return max(1, life + count * interval)


def _advance_fires(room, machine, now):
    """把所有还在烧的火墙推进到**此刻**，站在火上的人该掉血就掉（§78）。

    和 `_advance_shells()` 同一套路数：按**真实流逝的时间**算该推到第几个
    tick，每个 tick 问一次「谁站在**还活着的**火里」。

    ★★ 名单**不按碰撞组过滤**：火墙的组是 255 = 撞所有人（§69 /
    packet_api §5.4d），队友和 bot 自己都烧。
    ★★★ **闯关房里烧不着人**（§142）：`_battle_bodies()` 在那儿返回空表，
    这份名单只剩怪 —— 火墙照铺、照烧怪，队友和 bot 自己一点都不掉。

    ## ★★★ 再烧的账记在**人**身上，几道墙共用一本（§85）

    原版那道门是 `0x50f7a7`：时刻戳 `[角色+0x160]`，**一个角色只有一格**，
    每团火进来都查它、都改它，间隔恒 20 个 tick（`Flame` 构造里写死的
    `0x14`）。所以**不管站在几团火里、几道墙叠在一起，一个人 20 个 tick
    之内只会掉一次 `Damage`**。

    以前这本账记在 `FireWall` 上，两道墙叠起来能在同一个 tick 各烧一次
    （20 点）—— 原版做不到。这一轮把它挪到 `machine.burnt`（**按座位**），
    并且几道墙按**绝对 tick** 一起往前推，免得推进顺序影响判定。

    ⚠ 账是**每台机器一本**：原版那一格在射手本机的角色对象上，
    别人的火墙走别人的机器，互不相干（bot 之间也一样）。
    """
    if not machine.fires:
        return
    end = _clock_tick(now)
    start = min(wall.born_tick + wall.ticks + 1 for wall in machine.fires)
    # ★ 掉帧不该让火多烧几轮：最老那道墙的寿命就是往回追的上限。
    start = max(start, end - BOT_FIRE_CATCHUP_TICKS)
    # ★★ 怪也要烧（§134）：火墙的碰撞组是 255 = 撞所有人，闯关模式里
    #    bot 的燃烧瓶以前铺完火一只怪都烧不到。
    bodies = (_battle_bodies(room, machine.my_seat, include_self=True)
              + mob_bodies(room))
    for tick in range(start, end + 1):
        for wall in machine.fires:
            local = tick - wall.born_tick
            if local <= wall.ticks or local > wall.max_ticks:
                continue
            wall.ticks = local
            radius = wall.flame.size
            # ★ 夺分模式 ×2（§87）—— 用户拿火焰自己烧自己实测出来的那一条。
            damage = wall.flame.damage * _damage_scale(room)
            for seat_index, px, py, crouched, character_id in bodies:
                last = machine.burnt.get(seat_index)
                if last is not None and tick - last < BOT_FIRE_REBURN_TICKS:
                    continue
                character = (None if character_id is None
                             else chrprops.get(character_id))
                lit = _fire_touch(character, px, py, crouched, wall.flames,
                                  radius, local)
                if lit is None:
                    continue
                machine.burnt[seat_index] = tick
                wall.burnt[seat_index] = local
                burnt_mob = (seat_index[1] if isinstance(seat_index, tuple)
                             else None)
                who = (f"怪 {burnt_mob}" if burnt_mob is not None
                       else f"座位{seat_index}")
                machine.log(f"   火烧: {who} 在 "
                            f"({lit.x:.0f}, {lit.y:.0f}) 挨了 {damage} 点"
                            f"（第 {local} tick，火团句柄 {lit.handle}，§78/§85）")
                # ★ 火的击退是**常量** `(0, −8)`（§92，语料 1164 发无例外）。
                _emit(machine, machine.sync.event(
                    botsync.OP_SPLASH_DAMAGED,
                    botsync.splash_body(lit.handle,
                                        burnt_mob if burnt_mob is not None
                                        else botsync.character_handle(
                                            seat_index),
                                        damage, lit.x, lit.y,
                                        push_x=FIRE_KNOCKBACK[0],
                                        push_y=FIRE_KNOCKBACK[1])))
                if burnt_mob is not None:
                    _score_quest_damage(room, machine, damage)
                    continue
                _note_damage(room, seat_index, damage)   # ★ 血量台账（M5-C）
                _knock_back_seat(room, seat_index, damage, FIRE_KNOCKBACK,
                                 source="地面燃烧")
    # ★ 两条都算烧完了：推到头了，或者**这一刻它本来就该灭了**
    #   （服务端卡了一下、`BOT_FIRE_CATCHUP_TICKS` 那道闸没让它补完）。
    machine.fires = [w for w in machine.fires
                     if w.ticks < w.max_ticks
                     and w.born_tick + w.max_ticks > end]


def _body_circles(px, py, crouched, character_id):
    """这个身体的碰撞圆。★ `character_id is None` = 怪（§134）——
    句柄映射不回 `Mob.ini` 的类型，只有 `MOB_HIT_RADIUS` 那一个圆。"""
    if character_id is None:
        return ((float(px), float(py), MOB_HIT_RADIUS, "body"),)
    return chrprops.get(character_id).circles(px, py, crouched)


def _fire_touch(character, px, py, crouched, flames, radius, tick):
    """这个人有没有踩在**这一刻还活着**的某一团火里；踩着了返回那一团。

    `character` 传 `None` = 怪（按 `MOB_HIT_RADIUS` 那一个圆算）。
    """
    circles = (_body_circles(px, py, crouched, None) if character is None
               else character.circles(px, py, crouched))
    for cx, cy, r, _region in circles:
        for flame in flames:
            if not flame.alive(tick):
                continue
            if math.hypot(flame.x - cx, flame.y - cy) <= r + radius:
                return flame
    return None


def _set_ground_on_fire(room, machine, weapon, point, now):
    """★★ 火焰弹炸完补一发 `rpSetOnFire` —— 地上那道火墙（§75）。

    用户 2026-08-27 实机报的：「2 号角色，2 号武器扔在地上是会持续燃烧
    一会儿的，现在 bot 的没有燃烧。」

    根子和 `rpExplode` 是同一个（§42 / §72）：原版这一发是**射手那台机器**
    在 `IsMine` 门里发的，而 bot 的弹体在任何一台上都不是「自己的」
    ⇒ 没有一台会替它铺火。

    ⚠ **句柄记账必须跟着走**：这一发在收方吃掉 `2 × SpawnCount + 1` 个弹体
    句柄（`sync.set_on_fire()` 在同一次加锁里推进）。漏掉的话之后每一发
    `rpExplode` 都对不上号、被静默丢弃（§42）。
    """
    flame = _fire_wall_of(weapon)
    if flame is None:
        return
    handle = botsync.projectile_handle(machine.my_seat,
                                       machine.sync.projectiles)
    packet, step = machine.sync.set_on_fire(
        point[0], point[1], flame.id, flame.raw.get("spawn_count"))
    # ★★ 伤害归服务端（§78）：收方只把火画出来，算谁被烧的还是「射手那台」。
    terrain = _terrain(room)
    flames = _fire_wall_flames(terrain, flame, point, handle)
    machine.log(f"   火墙: 在 ({point[0]:.1f}, {point[1]:.1f}) 点着 "
                f"{flame.id}({flame.raw.get('section')}) "
                f"{len(flames)}/{step} 团火"
                f"（★ 收方吃掉 {step} 个弹体句柄，§75/§79）")
    _emit(machine, packet)
    # ★ 诞生时刻用**这一格的时刻**（D106），不是现取的挂钟：
    #   `_advance_fires()` 也是拿同一个 `now` 判「烧到第几 tick」，
    #   两处取不同的钟会让火墙的头尾各差半格。
    machine.fires.append(FireWall(
        handle, flame, flames, now, _fire_wall_ticks(flame)))


# ---------------------------------------------------------------------------
# ★★ 分裂弹的碎片（§81）—— 苹果雷炸开的那几片
# ---------------------------------------------------------------------------
def _slice_weapon_of(weapon):
    """这颗弹体炸开会不会分裂；会的话返回**碎片**那一节的武器记录（§81）。

    判据是**母弹自己写了 `SliceCount`**（`ch00-02` = 4、`ch03-02` = 3）。
    火焰弹也有 `SliceId`，但它没有 `SliceCount` —— 那条走的是火墙
    （`rpSetOnFire`），不是这里。
    """
    if not weapon.slice_count:
        return None
    slice_id = weapon.raw.get("slice_id")
    return None if not slice_id else weapondata.get(slice_id)


def _slice_angles(weapon, slice_weapon, roll):
    """`SliceCount` 片碎片各自的**发射角**（弧度），照抄 `0x47c9ae` 那个循环。

        度数 = SliceAngleBase + SliceAngle × i / (n − 1)
               + rand() % SliceAngleRandom − SliceAngleRandom / 2
        包里填的是它的**相反数**转弧度（`0x47ca03` 的 `fchs`）

    三个 `SliceAngle*` 读的是**碎片**那一节的定义（`ebx` 是按 `SliceId`
    查出来的那一份），苹果雷的 `ch00-02a` 三格都没写 ⇒ 用解析器里的缺省值
    160 / 30 / 30（`weapondata.Weapon.SLICE_ANGLE_DEFAULT` 那一组）。

    `roll(n)` 就是 `rand() % n`，单测拿它把随机数钉住。
    """
    count = int(weapon.slice_count)
    span = slice_weapon.slice_angle
    base = slice_weapon.slice_angle_base
    spread = slice_weapon.slice_angle_random
    out = []
    for index in range(count):
        degrees = base
        if count > 1:
            degrees += span * index // (count - 1)
        if spread > 0:
            degrees += roll(spread) - spread // 2
        out.append(math.radians(-degrees))
    return out


def _split_shell(room, machine, shell, point, victim_seat, tick):
    """★★ 苹果雷炸开的那几片碎片（§81）—— 每片一发 `rpFire` + 一颗 `Shell`。

    用户 2026-08-28：「1 号角色的 2 号武器苹果弹，真人玩的时候能看见敌人
    扔出去后炸裂开的几个碎片，现在看不到 bot 的炸裂碎片。」

    根子和火墙、溅射是同一个（§72）：造碎片那一段套在 `IsMine` 门里
    （`0x47c96e` 的 `call 0x50d294`），bot 的弹体在**任何一台**上都不是
    「自己的」⇒ 一片都不会生出来。

    ## 什么时候分裂

    收方是在**引信到期**那一 tick 分的（`0x47c952: dec [proj+0x338]` 数到 0
    → `OnHit(NULL, NULL)` → `IsMine` → 造碎片）；砸中角色的那一发弹体
    当场就没了，所以**不分裂**。语料对得上：2353 发苹果雷里
    「没打中角色」的 1837 发全部跟着 4 片碎片（飞了 12 发心跳 ≈ 1536 ms
    ≈ `SliceTime`），「打中角色」的 401 发一片都没有。
    ⇒ 服务端这边的判据就是 **`victim_seat is None`**。

    ## 句柄

    每一片都是收方一个真的弹体，**必须走 `sync.fire()`**（组包 + 记账在
    同一次加锁里），而且每一片之后都得有一发自己的 `rpExplode`
    —— `_advance_shells()` 会替它们发，所以这里把它们挂进 `pending_shots`。

    碎片的 `owner` 是射手，**碰撞组恒 255 = 撞所有人**（语料 7968 发一个
    例外都没有，和火墙同一个口径 —— `0x47ca0f: or eax, 0xffffffff`）。
    """
    slice_weapon = _slice_weapon_of(shell.weapon)
    if slice_weapon is None or victim_seat is not None:
        return
    fire_seq = machine.sync.events
    terrain = _terrain(room)
    # ★ 力度就是碎片那一节的 `Velocity`（`0x47ca12: fld [ebx+0x24]` 原样推
    #   进包里）。语料 7968 发碎片的 `+18` **恒 10.0**，而 `ch00-02a`
    #   的 `Velocity` 正是 10。
    power = float(slice_weapon.velocity or 0.0)
    for angle in _slice_angles(shell.weapon, slice_weapon, machine.roll):
        shot = ballistics.launch(slice_weapon, angle, power)
        # ★★★ 每一片在**开火那一刻**只吃 `shots` 个句柄（§86）。
        #   这里原来传的是 `handle_step`（总数 2），于是四片的号排成
        #   `base, base+2, base+4, base+6`，而收方给的是**连号**
        #   `base … base+3` —— 从第一次分裂起句柄就永久错开，
        #   之后每一发 `rpExplode` 都被静默丢弃：子弹照飞、一滴血不掉，
        #   换武器也救不回来（用户 2026-08-28 实机报的就是它）。
        packet, handle = machine.sync.fire(
            slice_weapon.id, point[0], point[1], shot.angle, shot.power,
            handle_step=slice_weapon.fire_step, shots=slice_weapon.shots,
            group=botsync.FIRE_GROUP_EVERYONE)
        _emit(machine, packet)
        max_ticks = _shell_max_ticks(terrain, shot, slice_weapon)
        for offset in range(slice_weapon.shots):
            machine.pending_shots.append(
                # ★ 碎片的时钟原点就是**母弹炸开的这一格**（D106）：
                #   收方也是在处理这一发 `rpFire` 的那一帧才建它们的。
                Shell(handle + offset, fire_seq, slice_weapon,
                      botsync.FIRE_GROUP_EVERYONE, point[0], point[1],
                      shot, _tick_moment(shell, tick), max_ticks,
                      born_tick=tick))
        fire_seq = machine.sync.events
    if not machine.split_logged:
        machine.split_logged = True
        machine.log(f"   分裂: 武器 {shell.weapon.id} 在 "
                    f"({point[0]:.1f}, {point[1]:.1f}) 炸成 "
                    f"{shell.weapon.slice_count} 片 {slice_weapon.id}"
                    f"({slice_weapon.raw.get('section')})，每片吃 "
                    f"{slice_weapon.handle_step} 个句柄（§81）")


def _tick_moment(shell, tick):
    """第 `tick` 格的**绝对时刻**（拿在飞的弹体当参照物换算）。

    弹体身上有「出膛的挂钟时刻 `born`」和「出膛在第几格 `born_tick`」两样，
    所以任意一格的时刻就是 `born + (tick − born_tick) × 32 ms`。
    火墙 / 碎片这些「在爆炸那一格诞生」的东西拿它当诞生时刻 ——
    比现取一次挂钟准，追赶时尤其（那时候挂钟已经跑到前面去了）。
    """
    return shell.born + (int(tick) - shell.born_tick) / ballistics.TICKS_PER_SECOND


def _advance_shells(room, machine, tick):
    """把所有在飞的子弹**往前推一格**（32 ms），撞上什么就当场结算。

    ## ★★★ 一格就是一格 —— 本模块最硬的一条时序（§147 / D106）

    收方对**远端弹体**每 32 ms 自己推一格，撞地形 / 引信到期就**本地自灭**；
    `rpExplode` 晚到一步就被 `0x492750` 静默丢弃：不扣血、不建溅射对象、
    **计数器不 +1**，而服务端照记 ⇒ 句柄从此永久错开、这个座位再也打不掉血
    （§42，一局之内不自愈）。以前这个函数挂在真人 ~128 ms 的心跳上，
    一次回补 4 格 —— **系统性地晚一个帧距**，落空的死法几乎必输。

    原版射手的口径（语料 2610 对实测，残差中位 +13 ms，不是 ±32）：

        弹体在 `rpFire` 发出的那一 tick 诞生（tick 0），此后每 32 ms 推一格，
        第 k 格上撞上，就在「出膛 + k×32 ms」当场发 `rpExplode`。

    ⇒ 推几格由 `tick − shell.born_tick` 说了算，和「上一次什么时候调的」
    没有关系。开火那一格 `tick == born_tick` ⇒ 一格都不推：收方也是在**下一个**
    tick 才推第一格的（所以旧代码 `_try_fire` 末尾那句「当场推一步」是**错的**，
    它让第 1 格早了整整 32 ms）。

    ## 一发都不能漏

    句柄记账在开火那一刻就推进了，少发一发 `rpExplode`，收方那一格计数器
    就和服务端错开，从此每一发都对不上号（§42）。所以这个函数排在
    `_tick_bot` 的最前面，**连「bot 这会儿正躺着」都不挡它**：
    真人死了，他打出去的子弹照样在飞。

    ## 命中判定用的是「此刻」的位置

    以前要在两帧之间**插值**（旧 §96），因为一帧要补 4 格、而人的位置只有
    真人心跳到达时才变。现在一格就是一格，「这一格人在哪」就是此刻手上
    最新的那份事实（真人那份由 `sim_body` 逐 tick 外推，和收方对远端角色
    做的事同一个口径，§39）—— 不再需要等未来的心跳回头插值。
    """
    if not machine.pending_shots:
        return
    # ★ 保险：事件序号只会往前走，除非换代把它清回 0（`_sync_epoch`）。
    #   真发生了的话这些记录是上一代的，句柄早就作废了 —— 丢掉，别拿过期的
    #   号去撞收方那个静默丢弃。
    alive = [s for s in machine.pending_shots if s.fire_seq < machine.sync.events]
    # ★★★ **结算过程中还会有新的弹体生出来**：分裂弹炸开的碎片就是在
    #   `_resolve_shell()` 里挂进 `pending_shots` 的（§81）。先把队列腾空，
    #   收尾时再和「还在飞的」拼回去 —— 直接 `pending_shots = still` 会把
    #   这一轮新生的 4 片**整个吞掉**，于是它们的 `rpExplode` 一发都不发，
    #   句柄从此永久错开（§42 那个静默丢弃）。
    machine.pending_shots = []
    terrain = _terrain(room)
    bodies_cache = {}
    still = []
    for shell in alive:
        bodies = bodies_cache.get(shell.group)
        if bodies is None:
            # ★ 组 255 = **撞所有人**（分裂弹的碎片就是这个组，§81）——
            #   「所有人」里含射手自己，和溅射、火墙同一个口径（§69 / D50）。
            #   ★ 闯关房里连这一组也撞不着人（§142）：`_battle_bodies()`
            #     在那儿返回空表，弹体只剩怪和地形可撞。
            bodies = _battle_bodies(
                room, machine.my_seat, shell.group,
                include_self=(shell.group == botsync.FIRE_GROUP_EVERYONE))
            bodies_cache[shell.group] = bodies
        # ★★★ 这一颗**现在**该走到第几格。正常一格一格走，`while` 只转一圈；
        #   房间循环落后时它一次追完（追赶时**一格都不许跳**，跳了那一格里
        #   弹体不推进、`rpExplode` 又变成迟到，§147）。
        want = min(tick - shell.born_tick, shell.max_ticks)
        landed = None
        while shell.ticks < want:
            landed = _shell_step(room, shell, terrain, bodies)
            if landed is not None:
                break
        if landed is None and shell.ticks < shell.max_ticks:
            still.append(shell)
            continue
        if landed is None:
            # 飞到头了什么都没撞上（打空 / 飞出图外）—— 在最后那一点炸掉。
            landed = ((shell.x, shell.y), None, None)
        _resolve_shell(room, machine, shell, landed[0], landed[1], landed[2],
                       tick)
    # ★ `still` 在前、这一轮新生的碎片在后 —— 顺序只影响下一格的推进次序，
    #   不影响句柄（那个在 `sync.fire()` 里就定死了）。
    machine.pending_shots = still + machine.pending_shots


# ---------------------------------------------------------------------------
# ★★ 近身冲刺攻击（§64）—— 真人双击左右方向键的那一下
# ---------------------------------------------------------------------------
#: 冲刺攻击的**第几式**。语料 4394 发 `rpDash` 的 `+2` **恒 0** ——
#: 真人打出来的就只有第 0 式，bot 照抄。
BOT_DASH_INDEX = 0

#: 一帧动画多久 —— 按**收方的逻辑步长**（32 ms）算。
#:
#: ⚠ 这是**假设**：`ChrProps.ini` 的 `CastEndFrame` / `DamageEndFrame` /
#: `TotalFrame` 是动画帧号，而那个动画一帧多久 ini 里没写、客户端那段也没逆。
#: 取 `TICK_MS` 的依据是数量级对得上（角色 0 的 `TotalFrame=25` ⇒ 一下 0.8 秒）。
#: ★ 它只影响**这一下打多久 / 什么时候判伤害**，不影响任何包的字节 ——
#: 差一点点的后果是「近身这下的节奏偏快偏慢」，不是静默故障。
BOT_DASH_FRAME_MS = ballistics.TICK_MS


class DashSwing(object):
    """一次**正在进行**的近身攻击。

    `rpDash` 只说「我冲了」，**伤害不在那一发里** —— 和弹体一样，
    判中和扣血是射手那台机器的活（D28），bot 没有本机 ⇒ 归服务端。
    命中之后补一发 `rpSplashDamaged`（§67）。
    """

    __slots__ = ("handle", "born", "direction", "move", "character_id",
                 "frames_done", "hit")

    def __init__(self, handle, born, direction, move, character_id):
        self.handle = int(handle)
        self.born = float(born)
        #: `+1` 左右：`-1` / `+1`。伤害圈的水平偏移跟着它翻。
        self.direction = int(direction)
        self.move = move
        self.character_id = int(character_id)
        #: 已经判过伤害的最后一帧。
        self.frames_done = -1
        #: 这一下已经打中过了吗（一下只打一次）。
        self.hit = False

    def frame_at(self, now):
        return int((now - self.born) * 1000.0 / BOT_DASH_FRAME_MS)

    def __repr__(self):
        return ("<DashSwing %d 方向%+d 第%d帧%s>"
                % (self.handle, self.direction, self.frames_done,
                   "已命中" if self.hit else ""))


def _stamina_props():
    return chrprops.game()


def _regen_stamina(machine, now, crouched=False, fast_run=False):
    """按**真实流逝的时间**补体力（`GameProps.ini` 的 `SpCharging`）。

    ★ 三个数全是原版的：每 tick 回 `SpCharging`（0.25），蹲下 **×2**
    （`0x507250`，§41），冲刺跑每 tick 花 `FastRunSpCost`（1.5）。
    没有一个是我拍脑袋的常量（铁律 10）。
    """
    props = _stamina_props()
    if machine.stamina is None:
        machine.stamina = props.sp_max
        machine.stamina_at = now
        return machine.stamina
    ticks = max(0.0, (now - machine.stamina_at) * ballistics.TICKS_PER_SECOND)
    machine.stamina_at = now
    gain = props.sp_charging * (2.0 if crouched else 1.0)
    if fast_run:
        gain -= props.fast_run_sp_cost
    machine.stamina = max(0.0, min(props.sp_max,
                                   machine.stamina + gain * ticks))
    return machine.stamina


def _dash_hits(room, swing, x, y, frame, bodies):
    """第 `frame` 帧时这一下打中了谁：`(座位号, 部位)`；没打中返回 `None`。

    伤害圈的位置按 `ChrProps.ini` 自己写的那条公式算（见 `chrprops.Move`），
    水平偏移跟着朝向翻。
    """
    offset = swing.move.offset(frame)
    px = x + offset[0] * (1.0 if swing.direction >= 0 else -1.0)
    py = y + offset[1]
    radius = swing.move.radius
    for seat_index, bx, by, crouched, character_id in bodies:
        if character_id is None:
            # ★ 怪（§129）：句柄映射不回 `Mob.ini` 的类型 ⇒ 没有三个碰撞圆，
            #   统一按 `MOB_HIT_RADIUS` 那一个圆算（和 `_mob_contact()` 同口径）。
            if math.hypot(bx - px, by - py) <= radius + MOB_HIT_RADIUS:
                return (seat_index, "body")
            continue
        region = chrprops.get(character_id).hit_region(
            bx, by, px, py, radius=radius, crouched=crouched)
        if region is not None:
            return (seat_index, region)
    return None


def _advance_dash(room, machine, now):
    """推进**正在进行**的那一下近身攻击，打中了就补一发 `rpSplashDamaged`。

    ★ 和子弹一样，判据是**物理**的：动作走到第几帧、那一帧的伤害圈在哪、
    圈里有没有人。一下只打一次（真人也是）。
    """
    swing = machine.dash_swing
    if swing is None:
        return
    if machine.battle_pos is None:
        machine.dash_swing = None
        return
    frame = swing.frame_at(now)
    if not swing.hit:
        # ★ 和 `_dash_target()` 用同一份名单（§129）：闯关房里那份只有怪。
        bodies = _melee_bodies(room, machine, machine.my_seat)
        x, y = machine.battle_pos
        first = max(swing.frames_done + 1, swing.move.cast_end)
        for step in range(first, min(frame, swing.move.damage_end) + 1):
            swing.frames_done = step
            landed = _dash_hits(room, swing, x, y, step, bodies)
            if landed is None:
                continue
            seat_index, region = landed
            swing.hit = True
            offset = swing.move.offset(step)
            # ★ 近身伤害也走同一条路（§87）：`0x481dfd` 那个 `[vft+0x128]`
            #   进门第一件事就是 `call 0x4806bf`。
            damage = swing.move.damage * _damage_scale(room)
            # ★ 近身的击退也是**常量**：`(朝向 × 15, −10)`（§92，语料 1347 发）。
            facing = 1.0 if swing.direction >= 0 else -1.0
            push = (DASH_KNOCKBACK[0] * facing, DASH_KNOCKBACK[1])
            mob_handle = (seat_index[1] if isinstance(seat_index, tuple)
                          else None)
            _emit(machine, machine.sync.event(
                botsync.OP_SPLASH_DAMAGED,
                botsync.splash_body(
                    swing.handle,
                    mob_handle if mob_handle is not None
                    else botsync.character_handle(seat_index),
                    damage,
                    x + offset[0] * facing,
                    y + offset[1],
                    push_x=push[0], push_y=push[1])))
            if mob_handle is None:
                _note_damage(room, seat_index, damage)   # ★ 血量台账（M5-C）
                _knock_back_seat(room, seat_index, damage, push,
                                 source="bot 近身")
                who = f"座位{seat_index} 的{region}"
            else:
                # ★ 怪没有血量台账、也不由服务端替它算击退（那是控制者那台
                #   机器的活）；打中的这一下只记进闯关分数（§130）。
                _score_quest_damage(room, machine, damage)
                who = f"怪 {mob_handle}"
            machine.log(f"   近身: 冲刺打中 {who}"
                        f" 伤害 {damage} 第{step}帧"
                        f" 句柄 {swing.handle}")
            break
    if frame >= swing.move.total_frame:
        machine.dash_swing = None


def _melee_bodies(room, machine, seat_index, targeting=False):
    """近身这一下**碰得着谁** —— 座位 + ★ 闯关房里的怪（V0.3 §129）。

    用户 2026-08-30：「闯关模式下，bot 似乎不会用近战招式，怪都贴脸了，
    bot 都不发动近战招式。」根因是 `_dash_target()` 原来只问
    `_hostile_targets()`，而**闯关房那张表按设计恒为空**（大家都是队友）
    —— 于是它永远返回 `None`，一下都冲不出去。

    怪的格式是 `(("mob", 句柄), x, y, False, None)`：第一格用元组，
    和 `_resolve_shell()` 里那套 `("mob", 句柄)` 是同一个约定；
    `character_id` 填 `None` 表示「没有三个碰撞圆，按 `MOB_HIT_RADIUS`
    那一个圆算」（句柄映射不回 `Mob.ini` 的类型，和弹体那边同一个口径）。

    ★★ `targeting` 分开两种口径，别混：

    * `True`（**要不要冲**）—— 只算**看得见的敌人**：挑目标是「决定」，
      屏幕外的人和烟里的人真人也挑不中（§127 / D67）；
    * `False`（**已经冲出去了，打中了谁**）—— 不过滤：这一下的伤害判定是
      **物理**的（圈里有没有人），和「我看不看得见他」无关。
    """
    group = _seat_group(room, seat_index)
    out = _battle_bodies(room, seat_index, group)
    mobs = mob_bodies(room)
    if targeting:
        hostile = set(t[0] for t in _visible_targets(room, machine,
                                                     seat_index))
        out = [b for b in out if b[0] in hostile]
        mobs = [row for row in mobs if _in_sight(machine, row[1], row[2])]
    return out + mobs


def _dash_target(room, machine, seat_index, move):
    """够得着的敌人（最近的那个）；没有返回 `None`。

    判据就是这一招**自己**的伤害圈：任何一个伤害帧的圈能盖住对方，
    就算够得着。够不着一步都不冲 —— 原版真人也不会对着空气双击。
    """
    if machine.battle_pos is None:
        return None
    x, y = machine.battle_pos
    bodies = _melee_bodies(room, machine, seat_index, targeting=True)
    if not bodies:
        return None
    best = None
    for direction in (botsync.DASH_RIGHT, botsync.DASH_LEFT):
        probe = DashSwing(0, 0.0, direction, move, machine.character_id)
        for frame in move.frames():
            landed = _dash_hits(room, probe, x, y, frame, bodies)
            if landed is None:
                continue
            target = [b for b in bodies if b[0] == landed[0]][0]
            span = abs(target[1] - x)
            if best is None or span < best[0]:
                best = (span, landed[0], direction)
            break
    return None if best is None else (best[1], best[2])


def _try_dash(room, machine, seat_index, now, on_ground):
    """够得着就来一下近身冲刺攻击（`rpDash`）。发了返回 `True`。

    ## 三个前提

    1. **踩在地上** —— 原版那一下是地面动作（`0x515b03` 那两段双击判定
       都在地面输入处理里）；
    2. **体力够** —— 花 `DashNN-SpCost`（角色 0 是 30，满体力 100）；
    3. **上一下打完了** —— 一次只能有一个 `DashSwing`。

    ⚠ 收方**不会**替远端角色扣体力（它只是播个动画），所以这里的体力是
    bot 自己给自己上的约束 —— 用户 2026-08-27 说的「消耗体力触发」就是它。
    """
    if not machine.melee or machine.dash_swing is not None or not on_ground:
        return False
    if machine.holding:
        return False                       # `/hold` 是「站住别动」，那就别冲
    move = chrprops.get(machine.character_id).dash(BOT_DASH_INDEX)
    if move is None or move.damage <= 0 or move.radius <= 0:
        return False
    if machine.stamina is None or machine.stamina < move.sp_cost:
        return False
    target = _dash_target(room, machine, seat_index, move)
    if target is None:
        return False
    target_seat, direction = target
    x, y = machine.battle_pos
    packet, handle = machine.sync.dash(direction, BOT_DASH_INDEX, x, y)
    machine.stamina -= move.sp_cost
    machine.dash_swing = DashSwing(handle, now, direction, move,
                                   machine.character_id)
    who = (f"怪 {target_seat[1]}" if isinstance(target_seat, tuple)
           else f"座位{target_seat}")
    machine.log(f"   近身: 冲刺 朝{'右' if direction > 0 else '左'} "
                f"目标 {who} {move!r} "
                f"体力 {machine.stamina:.0f}/{_stamina_props().sp_max:.0f}"
                f" 句柄 {handle}（★ 收方也吃掉一个弹体句柄，§64）")
    _emit(machine, packet)
    return True


def _try_fire(room, machine, seat_index, weapon, target, now, tick):
    """打一发 `rpFire`，把造出来的弹体挂进「在飞的子弹」队列。

    ## 为什么爆炸也得服务端发（§42）

    收方只替「自己的」和「中立/怪的」弹体做命中判定（`0x47eb4e` 那道守卫），
    bot 的弹体对**每一台**都是「别人的」⇒ 没有一台会替它算爆炸。
    只发 `rpFire` 的结果是子弹一直飞、永不落地、**一滴血都扣不掉**。

    ## 句柄

    `sync.fire()` 在**同一次加锁**里组包 + 推进记账，返回**头一颗**弹体的
    句柄 —— 那个号必须和收方 `mgr[0x14 + owner*4]` 当时的值一模一样，
    否则 `rpExplode` 会被**静默丢弃**（`0x492750` 查不到弹体就整个 return），
    表现是「子弹飞过去不炸」，而且一局之内不自愈。
    散射武器（`SpreadFrags > 1`）一发造好几颗，句柄是连号的（§46），
    所以每一颗都要挂一颗 `Shell`。

    ## 什么时候炸（§65 换掉了原来那套）

    ★★ 会话 18 之前是「按弹道算个飞行时间，到点了把爆炸点搬到目标身上、
    报命中」—— 那是**百发百中**，用户躲开了照样掉血、火箭炸在地上特效却
    出现在人身上。现在改成 `_advance_shells()` 逐 tick 跑真弹道，
    撞到人 / 撞到地形 / 飞出图外才炸，炸在**真的撞上的那一点**。
    """
    target_seat, point, shot = target
    x, y = machine.battle_pos
    # ★ 必须和 `_fire_target()` 解弹道时用的是**同一个**枪口（同一个 `point[0]`
    #   算出来的朝向），否则包里的发射点和弹道对不上。
    muzzle_x, muzzle_y = _muzzle(x, y, point[0])
    _declare_weapon(machine, seat_index, weapon)
    # ★ 这一发 `rpFire` 会拿到的**事件序号** —— 换代之后拿它认出「这颗是
    #   上一代的」（`_advance_shells` 开头那一道保险）。
    fire_seq = machine.sync.events
    # ★★★ 开火那一刻只吃 `shots` 个句柄 —— 带溅射的武器多出来的那一个是
    #   **爆炸那一刻**收方创建 `SplashDamage` 时分配的（§54 / §86），
    #   由 `_resolve_shell()` 的 `sync.explode()` 单独记。
    #   ⚠ `/noboom` 开着时不发爆炸，那一个自然也就不记 —— 两边天然一致，
    #     不再需要单独打补丁（用户 2026-08-27 踩过的那条路）。
    # ★★ 这一颗的倍率在**开火那一刻**定死（§117）：状态可能在它飞到一半
    #    时打完撤掉，可这一颗已经是放大过的了。
    damage_ratio, size_ratio = _magazine_ratios(machine)
    step = weapon.fire_step
    # ★★★ 碰撞排除组（§63）：**填错就是「明明躲开了还掉血」**。
    #   收方把它写进弹体的 `[proj+0x15c]`，和角色的一比，相同就整个跳过
    #   碰撞 —— 以前这一格被当成「武器槽」写死成 1，于是个人战里座位 0
    #   那个人身上一发都撞不着，而服务端自己发的 `rpExplode` 照样扣血。
    group = _seat_group(room, seat_index)
    # ★ 取证专用（`BOT_DIAG_FIRE_ANYWHERE`）：**每隔一发**把 `rpFire` 的
    #   owner 换成目标真人的座位。收方那边一发是「bot 打的」、下一发是
    #   「这个真人打的」，弹道完全一样、发射点完全一样 —— 屏幕上要是只看得见
    #   一半，那差别就锁死在 owner 上，和弹体、武器、时序全都无关。
    source_seat = None
    if BOT_DIAG_FIRE_ANYWHERE and 0 <= target_seat < len(room.seats):
        machine.diag_alt_source = not getattr(machine, "diag_alt_source", False)
        if machine.diag_alt_source:
            source_seat = target_seat
    packet, handle = machine.sync.fire(
        weapon.id, muzzle_x, muzzle_y, shot.angle, shot.power,
        handle_step=step, shots=weapon.shots, source_seat=source_seat,
        group=group)
    if source_seat is not None:
        machine.log(f"   ◆诊断 这一发用**座位 {source_seat}（真人）**的 owner 发出去")
    # ★ **每个距离档打一行**（按状态翻转去重，铁律 10 的口径；每发都打的话
    #   140 ms 一行会把日志刷爆）。句柄错位是整条链上**唯一**会静默失败的
    #   地方（`0x492750` 查不到弹体就整个 return），实机看到「子弹飞过去
    #   不炸」时，能对得上号的就只有这一行。
    mark = (weapon.id, int(shot.ticks))
    if mark not in machine.fire_logged:
        machine.fire_logged.add(mark)
        head, body = packet[:12], packet[12:]
        machine.log(f"   开火: 武器 {weapon.id}({weapon.section}) "
                    f"伤害 {weapon.damage}/头{weapon.head_damage}/腿"
                    f"{weapon.legs_damage} 弹体 {weapon.shots} 半径 "
                    f"{weapon.size} 溅射 {weapon.splash_range} 步进 {step}；"
                    f"节奏 弹匣 {weapon.magazine} 发 × 冷却 "
                    f"{weapon.cooling_ms}ms + 换弹 {weapon.reload_ms}ms"
                    f"（无弹匣的话按 {weapon.fire_interval_ms}ms 一发）；"
                    f"弹道 {shot!r}；弹体句柄 {handle}；碰撞组 {group}；"
                    f"瞄 座位{target_seat} ({point[0]:.0f}, {point[1]:.0f})"
                    f"；头 {head.hex()} body({len(body)}) {body.hex()}")
    _emit(machine, packet)
    # ★ 捡来那把枪的发数在**开火那一刻**减一（原版 `0x48bade`，就挂在
    #   创建弹体的前一步 `0x5151dd`）。到 0 之后由 `_expire_item_weapon()`
    #   下一帧换回自己那把 —— 和原版一样，是「刷新武器」时才结算。
    _spend_item_weapon_shot(machine)
    # ★★ 强力射击 / 三重射击 / 毒弹这三条状态同样按**发**消耗（§117）。
    #    打完就地撤掉并广播 `0x040d` —— 不发的话别人屏幕上永远不结束。
    _spend_magazine_shots(room, machine, seat_index)
    terrain = _terrain(room)
    max_ticks = _shell_max_ticks(terrain, shot, weapon)
    for offset in range(weapon.shots):
        # ★★★ `born_tick` = **发这一发 `rpFire` 的那一格**（D106 / §147）。
        #   收方是在处理这一发的那一帧建弹体、**下一帧**才推第一格，
        #   我们跟着同一个相位，`rpExplode` 就恒在「出膛 + k×32 ms」发出。
        shell = Shell(handle + offset, fire_seq, weapon, group,
                      muzzle_x, muzzle_y, shot, now, max_ticks,
                      born_tick=tick)
        shell.damage_ratio = damage_ratio
        shell.size_ratio = size_ratio
        machine.pending_shots.append(shell)
    machine.next_fire_at = _reload_after_shot(machine, weapon, now)
    # ★★ 这一发的瞄准失误用掉了 —— 下一发重掷（M5-D）。判据是「打了一枪」
    #    这个事件，不是「过了多久」（铁律 10）。
    _reroll_aim_miss(machine)
    # ★ 松手 = 蓄力清零（`0x51685c: and [char+0x594], 0`，§73）。
    #   下一颗手雷得从头按起。
    machine.charge_at = None
    # ★★★ **这里绝不能当场推一步**（D106 / §147）。旧代码为了让贴脸那一发
    #   早点炸，在这儿补了一次 `_advance_shells()` —— 那让弹体的第 1 格
    #   在 t=0 就结算，比收方早整整 32 ms。语料实测（2610 对）原版射手是
    #   「出膛那一格记 0，第 k 格撞上就在出膛 + k×32 ms 发包」，
    #   贴脸那一发也不例外。现在房间循环 32 ms 就来一格，本来也不用抢。
    return True


def _battle_started(room):
    """这一局的战斗**真的开始了吗**（全员加载完、进了 stage 7）。

    `room.battle.state == IN_GAME` 由 `0x0402` 推进，而
    `reset_sync_trails()`（清轨迹 + 清 bot 的战斗帧）就挂在同一处 ——
    所以「进了 IN_GAME」等价于「这一局的状态已经重新起过一份」。
    ★ 拿不到 `battle` 的场合（控制通道造的假房间）按**已开始**算，
    别把那条测试路径挡死。
    """
    battle = getattr(room, "battle", None)
    state = getattr(battle, "state", None)
    if state is None:
        return True
    return state == gameserver.StartGameHandshake.IN_GAME


def _fell_out_of_the_world(room, machine, terrain):
    """★★★ 这个 bot 是不是**掉出地图下边界**了（§143）。

    照抄客户端每帧那一下 `Character::CheckFallDown`（`0x50d520`）：

        这张图的 map.ini 记录有 `FallDown` 吗？没有 -> 掉下去不死；
        角色底部 y + 5 >= 地图高度 ?  -> ProcessFallDown()

    ★ 服务端的 `body.y` 就是**落脚点**（和客户端 `[char+0x38]` 同一口径，
      `chrprops.center()` 是拿它减去身体半高算出来的），所以直接和图高比。

    ★★ 为什么 bot 会「站」在图外：`mapdata` 里**出界返回 2**（照抄客户端，
      免得 bot 觉得图外能走）—— 于是掉下去的 bot 被图外那圈虚拟实心接住，
      悬在最后一行上，既不死也回不来。用户 2026-08-30 报的
      「bot 掉到岩浆里不会死亡，还在空中不停上下抽搐，然后进度卡住」
      就是这个：**判死这一条以前整条不存在**。
    """
    body = machine.body
    if body is None or terrain is None or room is None:
        return False
    if not mapdata.falls_out_of_the_world(getattr(room, "map_name", "")):
        return False
    return body.y + mapdata.FALL_DOWN_MARGIN >= terrain.height


def _report_fall_death(room, machine, seat_index):
    """替掉出去的 bot 走一遍**真人那条死亡路**（§143）。

    原版是角色本机 `PlayerCharacter::ProcessFallDown`（`0x51503a`）**直接**
    `GameContext::ReportDeath`（`0x493855`）发一发 `0x0408` —— 不扣血、
    不经过伤害那一路。bot 没有本机，服务端照着造一发同样的包，喂给
    `Conn.on_report_hp_zero`：广播 `0x0406`、上重生闩、记分**一条都不用
    重写**，和「别人打死它」走的是同一段代码（D3 那条放宽本来就为它开的）。

    ★★ **凶手那一格填自己的座位**（= 自杀），不是原版那个 0：原版的 0 是
      `[char+0x158]`「没人打过我」的默认值，而服务端这边 `record_kill()`
      会把它当**座位号 0** —— 照填等于每次掉坑都给 0 号位白送一个人头。
      填自己 = 自杀，扣自己一分（§224），账才是对的（D104）。
    """
    quest = None if room is None else room.quest
    handle = botsync.character_handle(seat_index) & 0xFFFFFFFF
    # ★ 「我死之前死过几次」要报**服务端权威计数**：`record_death()` 的第 2
    #   层判重是 `报的值 + 1 <= 已广播次数`，恒填 0 的话第二次掉坑就被吃掉。
    reported = 0
    if quest is not None:
        reported = int(getattr(quest, "death_counts", {}).get(handle, 0))
    body = machine.body
    payload = struct.pack(gameserver.DEATH_REPORT_FORMAT, handle,
                          seat_index & 0xFF, seat_index & 0xFF, reported,
                          float(body.x), float(body.y))
    machine.log(f"   ★掉出地图: ({body.x:.0f}, {body.y:.0f}) "
                f"—— 这张图 FallDown=1，照原版判死")
    try:
        machine.on_report_hp_zero(payload)
    except (OSError, ValueError, struct.error) as error:
        machine.log(f"   ⚠ 掉落判死上报失败（{error!r}）")


def _lying_dead(room, seat_index):
    """这个座位现在是不是「躺着」—— 等重生，或者命用完了这一局不再起来。

    两种都不该发心跳：真人死了也不发。第二种是 V0.3 §34 —— 看门狗判完
    「三条命用完」之后会把座位记进 `quest.lives_spent`，闩同时也撤掉了，
    只看 `respawn_due` 的话 bot 会**以幽灵的姿态继续跑**。
    """
    quest = room.quest
    if quest is None:
        return False
    return (seat_index in quest.respawn_due
            or seat_index in getattr(quest, "lives_spent", ()))


def _tick_bot(room, machine, seat_index, tick, now, behind=0):
    """一个 bot 走**一格**（32 ms）。

    ## ★★★ 一格 = 收方的一个物理 tick（D106，废止 D17 / §32）

    以前这里是「一帧 = 我跟的那个真人报了一个新位置」，节奏 ~128 ms。
    可收方对**远端弹体**是每 32 ms 推一格、撞地形就本地自灭的 ——
    跟着真人走，`rpExplode` 系统性地晚一个帧距，收方那份弹体已经没了、
    包被 `0x492750` 静默丢弃，弹体句柄计数器从此永久错开，这个座位
    「子弹照飞、一滴血不掉」（§147，2026-08-31 实机 bot3 整局零伤害）。

    这一格里按收方的顺序做这些事：

    1. 在飞的子弹推一格 —— 撞上什么就**当场**发 `rpExplode`（排在最前面，
       它的发出时刻不许被走位和 AI 的耗时推迟）；
    2. 地上的火 / 近身那一下 / 捡来那把枪的额度，各自按时刻结算；
    3. 走一格 `botmove.tick`；
    4. 事件包（`rpJump` / `rpCrouch` / `rpFire`）在**发生的那一格**发；
    5. 每 `HEARTBEAT_TICKS` 格发一发心跳 —— 那是真人客户端自己的节奏。

    ★ AI 决策（换枪 / 挑目标 / 走位意图）不在每一格上跑，见 `_decide()`。
    """
    if machine.sync.broken:
        return
    if not _battle_started(room):
        # ★★ **`0x0400` 到 `0x0402` 之间一动不许动**（用户 2026-08-27 的日志
        #   里抓到的）：`0x0400` 一广播 bot 就被标记成「加载完」（D4），
        #   而**真人还在读图**。这时候 `reset_sync_trails()` 还没跑（它挂在
        #   `IN_GAME` 上，要等 `0x0402`），于是 bot 会拿着**上一局残留的轨迹
        #   和句柄计数器**开枪 —— 收方那边换图时已经把计数器清了，
        #   两边从此对不上号（§42 那个静默丢弃）。
        #   ⇒ 判据是**房间真的进了 stage 7** 这个事件（`0x0402`），不是定时器。
        return
    #: 这一格发不发心跳。事件包**不看它** —— 那些在发生的那一格就发。
    #: ★ 落在这一组的**最后一格**：心跳报的位置因此就是这 4 格走完之后的
    #:   位置，和真人客户端「跑完这一帧再发」是同一个口径。
    # ★★★ **追赶途中不报位置**（V0.3 §153 / D115）：房间循环落后时会把欠的
    #   格一口气补完（`RoomLoop._run`，这是对的 —— 弹体一格都不许跳，§147），
    #   可要是每 4 格照发一发心跳，落后 38 格就是**9 发心跳挤在几毫秒里**，
    #   而在落后的那一秒多里一发都没有。收方在静默期一直拿最后那份按键掩码
    #   替 bot 走（`0x507660`，最高 ~690 px/s），恢复时被一把拽回去 ——
    #   屏幕上就是「bot 瞬移一段距离」。实机量到过「落后 38 格（1216 ms）」
    #   ≈ 840 像素，和用户看到的量级对得上。
    #   ⇒ 追平的那一格才报。这正是原版客户端卡了一秒之后的行为：
    #     恢复时发**一发**位置包，不是十发。事件包不受影响 —— 那是账本。
    #   ★ 判据是「还欠不欠格」这个事实，不是「落后超过 N 格」的阈值（铁律 10）。
    due = (tick % gameserver.HEARTBEAT_TICKS) == (
        gameserver.HEARTBEAT_TICKS - 1)
    if behind > 0:
        # 还欠着格 = 正在追赶。这一发**欠下来**，追平的那一格一起还。
        machine.beat_pending = machine.beat_pending or due
        beat = False
    else:
        beat = due or machine.beat_pending
    try:
        # ★★ **排在所有分支前面**：在飞的子弹一发都不能漏（见那个函数的
        #   注释）。bot 躺着、`/hold` 着 —— 都不影响「上一发子弹该炸了」
        #   这个事实，而它的**发出时刻**就是句柄账的全部（§147）。
        _advance_shells(room, machine, tick)
        # ★ 地上的火同理：它烧不烧得到人，和 bot 躺没躺着无关（§78）。
        _advance_fires(room, machine, now)
        # ★ 正在进行的那一下近身攻击同理：它的伤害判定是**物理**的
        #   （动作走到第几帧、圈里有没有人），和 bot 躺没躺着无关。
        _advance_dash(room, machine, now)
        # ★ 捡来那把枪的额度同理：「15 秒到了」是**时间**决定的事实，
        #   和这一格 bot 在不在跑无关（§115）。
        _expire_item_weapon(room, machine, seat_index, now)
    except botsync.SyncInvariantError as error:
        machine.sync.broken = True
        machine.log(f"   ★★ 同步流不变式被破坏，已停掉这个 bot 的同步: {error}")
        return
    # ★★ 「躺着没有」这件事**在死亡分支前面**记（§74）：那道 2 秒锁的
    #   起算点是「躺着 -> 站起来」这个**翻转**，翻转的前一半发生在
    #   bot 还躺着的时候。放到后面记的话 `was_lying` 永远是 False，
    #   复活那一格看不到翻转，锁也就永远不会重新挂上。
    _note_action_lock(room, machine, seat_index, now)
    if _lying_dead(room, seat_index):
        # ★ 死亡处理器 `0x4ffbb7`（虚槽，`Character` / `MyCharacter` 同一格）
        #   里有一句 `mov byte [ebx+0x2b5], 0` —— **客户端一死就把蹲的状态
        #   清掉了**。这边不跟着清的话，两边的记账从此错开一轮：真人还蹲着
        #   时 bot 看不到「翻转」，于是重生后**不发**那一发蹲下（§41）。
        machine.crouched = False
        # ★ 手指也松开：躺着的时候鼠标按下去没有任何意义（§73）。
        machine.charge_at = None
        # ★ 捡来的枪也丢掉：真人死了重生就换回自己那把（§223 的 `GiveWeapon`
        #   是「换成这把」，重生时 `Character::Reset` 把武器恢复成角色本来的）。
        machine.drop_item_weapon()
        # ★ 按发数算的状态也清：死一次属性表就空了（`Character::Reset`），
        #   每台客户端自己会拆掉，这边不用补 `0x040d`。
        machine.magazine_attrs = {}
        # ★ 减速 / 冰冻也一样：死一次身上的状态就清了（`Character::Reset`）。
        machine.slowed_until = None
        machine.frozen_until = None
        # ★★ 三张倒计时表跟着重上（§126）：原版 `Character::Reset`
        #   （`0x514565`）对**当前这把**枪同时上 `LoadingTime` / `ReloadTime`
        #   / `CoolingTime`，冻着的那几把整个作废。站起来那一刻要重新上膛。
        machine.weapon_cd = {}
        machine.weapon_rounds = {}
        machine.rounds_left = None
        # ★ 上一份决策跟着作废：躺下之前挑的目标和走位这会儿都不作数了。
        machine.intent = None
        machine.intent_tick = None
        machine.aim = None
        current = machine.weapon
        machine.next_fire_at = now + (
            0.0 if current is None else float(current.loading_ms or 0) / 1000.0)
        return

    leader = _follow_target(room, machine)
    if leader is None:
        # 房里还没有任何真人报过位置 ⇒ 我们**不知道**地图上哪里能站。
        # 这时候一发都不发：与其把 bot 摆到一个可能在地形里 / 图外的点上，
        # 不如让客户端按自己加载出来的出生点继续画着（D16）。
        return
    # ★★ 道具（V0.3 §100）：踩到就捡、捡到就用。「踩到了没有」是**位置**
    #    决定的事实，逐格问一次。非道具模式下 `items_at` 恒空，等于没开销。
    if _item_pickups(room, machine, seat_index):
        _use_held_item(room, machine, seat_index)
    # ★ 别人的道具：减速胶水踩上去要真的慢（§101/§105）、
    #   冰冻圈里要真的动不了（§106）。
    _step_on_slow_mine(room, machine, seat_index, now)
    _take_freeze(room, machine, seat_index, now)

    # ★★ 「同一帧」的缓存键（`_dodge_intent` / `nav_planned_at` 拿它去重）。
    #    一「帧」= 一发心跳的那 4 格 —— 就是 D106 之前真人心跳的那个节奏，
    #    所以躲避和递单的频率一点没变（那两件事都很贵）。
    machine.frame_seq = tick // gameserver.HEARTBEAT_TICKS

    # ★★★ **重生：先把身体搬到服务端选好的那个出生点**（§91）。
    #   看门狗补 `0x0419` 时已经把坐标记进 `pending_spawn` 并发给了客户端，
    #   客户端那边模型已经站在那儿了 —— 这边不跟着搬，下一发心跳就把它
    #   拽回死亡地点（用户 2026-08-28 报的「瞬移 + 抽搐」的另一半）。
    #   ★ 那个点是**已经落过地**的（`pick_respawn_point` settle 过），
    #     所以这里直接当「站在地上」建 `Body`，不用再查一次地形。
    #   ★ 放在 `/hold` 分支**前面**：钉住的 bot 也一样会死、一样要站起来。
    spawn = machine.pending_spawn
    if spawn is not None:
        machine.pending_spawn = None
        machine.battle_pos = (spawn[0], spawn[1])
        machine.body = botmove.Body(spawn[0], spawn[1], on_ground=True)
        _clear_navigation(machine)
        machine.intent = None

    terrain = _terrain(room)
    # ★★ AI 决策约 15 Hz（`BOT_DECISION_TICKS` 格一次），**不是每格**：
    #    寻路 / 解弹道 / 评估威胁又贵又不需要那么勤，真人也不会 32 ms
    #    换一次主意。物理照样每一格走。
    #    ★ 房里几个 bot 的决策**按座位错开**（`+ seat_index`）：不错开的话
    #      它们全挤在同一格上，那一格的耗时就是几倍 —— 而 `rpExplode` 的
    #      发出时刻就吃在这上面（§147）。实测一个 bot 的决策格 0.54 ms、
    #      普通格 0.20 ms（32 ms 的预算），错开之后峰值不随 bot 数叠加。
    if (machine.intent_tick is None
            or (tick - machine.intent_tick >= BOT_DECISION_TICKS
                and (tick + seat_index) % BOT_DECISION_TICKS == 0)):
        _decide(room, machine, seat_index, terrain, now, tick)

    if machine.holding and machine.battle_pos is not None:
        # ★ `/hold`：站在原地不动（用户 2026-08-26 要的测试手段）。
        #   **照常发心跳** —— 真人站着不动时也一直在发，停发反而是异常状态。
        #   姿势按「站着」来：踩地、速度 0、不按键、不冲刺、不蹲。
        #   ★ 开火那一段照跑：站住正是为了让房主走开、绕到墙后，
        #     看 bot 隔着地形还打不打得到（`line_blocked`，§29）。
        x, y = machine.battle_pos
        jumped, on_ground, vx, vy, fast_run, crouch = 0, True, 0, 0, False, False
        machine.move_down = False
        from_trail = False
    else:
        # ★★ **自己走位**（M5 / §71）：对战房里 bot 按地形自己挪，
        #   拿不到地形时 `_own_step()` 会返回 None —— 那就退回 D16 那条老路，
        #   回放真人的轨迹。
        point = _own_step(room, machine, seat_index, terrain, now, tick)
        from_trail = point is None
        if from_trail:
            machine.move_down = False
            rank = room.bot_seats().index(seat_index) + 1
            point = trail_point(leader.sync_trail, BOT_FOLLOW_DISTANCE * rank)
            # ★★ 轨迹点 8 Hz 才换一个 —— 那一步的方向记下来，下面四格的
            #    按键掩码都按它报（见 `trail_heading` 的说明）。判据是
            #    「真人报了新位置」这个事件，所以真人站住时它自然归 0。
            mark = (room.seat_index_of(leader), leader.sync_trail_seq)
            if point is not None and mark != machine.trail_mark:
                machine.trail_mark = mark
                machine.trail_heading = _walk_direction(machine.battle_pos,
                                                        point[0])
        if point is None:
            return
        x, y, jumped, on_ground, vx, vy, fast_run, crouch = point

    previous = machine.battle_pos
    machine.battle_pos_prev = previous
    machine.battle_pos = (x, y)
    # ★ 记下这一格报出去的地面标志：别人那台机器上 bot 的 `[char+0x128]`
    #   就是它，而夺分模式的一条 ×0.75 按受害者这一格判（§89）。
    machine.on_ground = bool(on_ground)
    # ★★★ **掉出地图下边界 = 死**（§143）：排在发心跳前面 —— 真人死了
    #   那一格也不发心跳，而且这一发要是发出去了，别人屏幕上的 bot 会先
    #   闪到图外再倒下。
    if _fell_out_of_the_world(room, machine, terrain):
        _report_fall_death(room, machine, seat_index)
        return
    direction = _walk_direction(previous, x)
    if not direction and from_trail:
        direction = machine.trail_heading
    if direction:
        machine.heading = direction
    # ★ 只有**踩在地上**才说「我按着方向键」：腾空那一段的动画是 `Jump`
    #   （不看掩码），而收方会拿按键覆写空中速度，把抄来的抛体速度冲掉（§39）。
    keys = botsync.walk_keys(direction if on_ground else 0)
    if machine.move_down:
        machine.down_latch = True
    if machine.down_latch:
        keys |= botsync.KEY_DOWN
    # ★★ 冲刺位（§40）：**在地上、真的在走**才算数（`0x515ced` 进冲刺就要求
    #   走路方向非 0），否则会出现真客户端里不存在的组合（站着冲刺）。
    horizontal_keys = keys & (botsync.KEY_LEFT | botsync.KEY_RIGHT)
    fast_run = bool(fast_run) and bool(horizontal_keys)

    # ★ 起跳**按状态翻转去重**：只有「这一格真的往前挪了」才补 `rpJump`
    #   （铁律 10 说的那种去重口径）。`rpJump` 是**事件包**，每发都要吃掉
    #   一个可靠序号，动画上还会抽。
    moved = previous is not None and (x, y) != previous
    try:
        if jumped and moved:
            # ★ 事件包（内层 < 0x4000）：序号必须严格连续，所以它和心跳里的
            #   N 是同一本账，全在 `BotSyncStream` 里记（D5）。
            #   ★★ 在**起跳的那一格**就发，不等下一发心跳 —— 原版射手也是
            #      这样，而收方按到达顺序处理事件包（D106）。
            _emit(machine, machine.sync.event(
                botsync.OP_JUMP, botsync.jump_body(seat_index, jumped)))
        # ★★ 蹲：心跳里没有这一位，只有 `rpCrouch` 这一发事件包说得着（§41）。
        #   所以**按状态翻转发**（铁律 10 的口径）：和上一格不一样才发一发。
        if bool(crouch) != bool(machine.crouched):
            _emit(machine, machine.sync.event(
                botsync.OP_CROUCH, botsync.crouch_body(seat_index, crouch)))
            machine.crouched = bool(crouch)
        weapon = machine.weapon
        # ★ 决策那一半挑好的目标（最多 2 格旧）：喂心跳里的准星，
        #   以及「这一格该不该扣扳机」。真开枪时会重解一次弹道，见下面。
        target = machine.aim
        cursor = None if target is None else target[1]
        # ★★ 手指按不按着，要在**组心跳之前**算好：心跳 `+15` 那一格就是
        #   蓄力计数器（`[char+0x594]`，packet_api §5.5）—— 报了它，别人
        #   屏幕上才看得见 bot 在攒力气（§73）。
        #   ★ 锁着的那 2 秒里连按都不按（§74）：真人那两秒点鼠标没反应。
        acting = _may_act(machine, now)
        _hold_trigger(machine, weapon, target if acting else None, now)
        # ★★ 体力：先按这一格的姿势结算（蹲着回得快、冲刺跑要花），
        #   再决定近身那一下打不打得起。三个速率全是 `GameProps.ini` 的。
        _regen_stamina(machine, now, crouched=bool(crouch), fast_run=fast_run)
        if beat:
            # ★★ 地面标志和速度**原样抄这一格算出来的**（§35），不从位移反推。
            # ★★★ 按键掩码是**走路动画的开关**（§39）：填 0 的话收方画站姿、
            #   而且不替它走，位置只被心跳一格一格地拉过去。
            # ★ 准星不传 = 摆在自己正前方（`aim_point`），朝向位和角度跟着它
            #   一起算（§36 / §37）。真人的身体朝向就是这么来的。
            state = botsync.character_state(
                x, y, vx=vx, vy=vy, on_ground=on_ground,
                facing=machine.heading, keys=keys, fast_run=fast_run,
                cursor=cursor, state_byte=_charge_value(machine, now))
            _emit(machine, machine.sync.heartbeat(state))
            machine.beat_pending = False
            # ★ ↓ 报出去了就把锁松开（见 `down_latch`）。
            machine.down_latch = False
        if BOT_DIAG_FIRE_ANYWHERE:
            _diag_why_not_firing(room, machine, seat_index, weapon, target, now)
        # ★★ **近身冲刺攻击优先于开枪**（§64）：原版这一下会占住整个角色
        #   （`TotalFrame` 那么多帧），真人也开不了枪。够得着就冲，够不着才打枪。
        dashing = (acting
                   and not (target is not None and target[0] == BREAKABLE_SEAT)
                   and _try_dash(room, machine, seat_index, now, on_ground))
        if (acting and not dashing and machine.dash_swing is None
                and target is not None and now >= machine.next_fire_at
                and _may_fire(machine, weapon)):
            # ★★★ **真扣扳机的这一格重解一次弹道**（§62 / D106）：`rpFire` 里
            #   带的是自己的枪口坐标，它必须和刚刚走完这一格的位置一致；
            #   而 `machine.aim` 那一份最多是 2 格之前算的。只有真要开枪的
            #   那一格才多算这一次 —— 开火间隔 ≥200 ms，开销可以忽略。
            fresh = _fire_target(room, machine, seat_index, weapon,
                                 miss=_aim_miss(room, machine, seat_index))
            if (fresh is not None
                    and _charge_ready(machine, weapon, fresh[2], now)):
                _try_fire(room, machine, seat_index, weapon, fresh, now, tick)
    except botsync.SyncInvariantError as error:
        # ★ 不变式炸了：把**这一个 bot** 的流停掉，别的人一点不受影响（D1）。
        #   继续发只会把收方的收包队列越弄越乱，而「bot 不动」是个看得见的故障。
        machine.sync.broken = True
        machine.log(f"   ★★ 同步流不变式被破坏，已停掉这个 bot 的同步: {error}")


def report_bots_loaded(room, why, confirmed=False, who=None):
    """房里每个 bot 广播一发 **`0x4005` = 100**「我这边已经加载完了」（D26）。

    ★★ **bot 的进度条一开始就是满的，这不是偷懒，是事实**：bot 没有客户端、
    没有一个字节的资源要读，它永远是房里加载最快的那个 —— D4 定的就是
    「`0x0400` / `0x0417` **广播出去的那一刻**它就算加载完了」。
    进度条那一格画的是 `0x4005` 的百分比（§30），所以那一刻直接报 100。

    ★ 之前那版是「跟着真人报的百分比画」（D23）。它在 1v1 下几乎画不出东西
    —— 客户端 `0x4005` 发侧有 **1000 ms 节流**（§38），房里唯一的真人一两秒
    读完图就只报了一两发。用户 2026-08-26 拍板：**别演了，直接 100**。

    调用点是两处**事件**，和 D4 那两处标记「bot 已加载完」的地方成对：
    刚广播完 `0x0400`（开局）、刚广播完 `0x0417`（换图）。

    ★★ 一轮里有两类**状态翻转**：

    1. `confirmed=False`：`0x0400` / `0x0417` 刚广播，立即报一次（整房
       一发，`load_progress` 记着）；
    2. `confirmed=True`：**某一个真人**（`who`）用自己的 `0x4005` 或加载
       完成请求证明「我这台的 LoadingStage 已经建好了」，替他确认重画。

    ★★★ 第 2 类为什么按**连接**去重、而不是整房一次：加载界面是每台
    客户端**各自**建的（§158），而唯一那发 100 和 `0x0400` 走同一条
    TCP 流、早了一帧，收侧因为座位对象还不存在整包丢掉（§38）。只按
    「整房补过一次」去重的话，界面建得比第一个人晚、自己又因为加载太快
    没发过进度包的那一个，仍然会看到 0%。每个真人的**第一个**加载事件
    各补一发，一局最多几发，仍然全是事件驱动、不吃可靠序号、不开计时器。

    `reset_battle_frame()` 在下一轮加载开始时把两本账一起清掉。
    `0x4005` 是可丢的立即包，重画同一个 100 既不吃事件序号，
    也不改收包队列。
    """
    # ★★ 血量台账整本清空（M5-C）：`0x0400` / `0x0417` 广播那一刻，
    #    每台客户端都把角色重建成满血 —— 和 `reset_battle_frame()` 同一个事件。
    ledger = _health(room)
    if ledger is not None:
        ledger.clear()
    for index in room.bot_seats():
        seat = room.seats[index]
        machine = None if seat is None else seat.conn
        if not isinstance(machine, BotConn) or machine.sync.broken:
            continue
        if confirmed:
            if who in machine.load_progress_confirmed:
                continue
        elif machine.load_progress == botsync.LOAD_PROGRESS_MAX:
            continue
        try:
            if confirmed:
                machine.load_progress_confirmed = (
                    machine.load_progress_confirmed | {who})
            else:
                machine.load_progress = botsync.LOAD_PROGRESS_MAX
            _emit(machine, machine.sync.volatile(
                botsync.OP_LOAD_PROGRESS,
                botsync.load_progress_body(botsync.LOAD_PROGRESS_MAX)))
        except Exception as error:          # noqa: BLE001 —— 同 `tick_room`
            machine.sync.broken = True
            machine.log(f"   ⚠ bot 进度条出错（{why}），已停掉它的同步: {error!r}")
    # ★★ 只在加载刚开始时预热。确认性重发只是补画进度条，
    #   不应该顺手又走一遍预热登记。
    if not confirmed:
        warm_navigation(room, why)


#: 预热线程的登记表：`地形对象 -> {角色尺度}`。**只防重复开线程**，
#: 不是缓存本身 —— 真正的缓存在 `botnav` 里。
#: ★ 弱引用键，和 `botnav._EDGE_CACHE` 同一个理由：地形一被丢掉，
#:   这份登记跟着没，下次重新加载会重新预热。
_WARMED = weakref.WeakKeyDictionary()
_WARM_LOCK = threading.Lock()

#: ★★★ 预热的活**全进程只有一条线程在干**（V0.3 §163）。
#:
#: 以前是「每个 (地形, 角色尺度) 各起一条线程」，破坏物一翻就是两条，
#: 每条把整张图重算一遍（`Iceria00` 实测 2.2~2.6 秒的纯 Python）。
#: CPython 只有一把 GIL —— 两条算力线程一起转的时候，每条连接那条
#: **发送线程**（D108）就被饿着：它每写一个包都要重新抢一次 GIL。
#: 实测（32 ms 醒一次的探针线程，`Iceria00` 真图）：
#:
#:     什么都不跑        中位 0.3 ms   p95   0.3 ms   最大   0.3 ms
#:     两条预热并行      中位 16.9 ms  p95  76.1 ms   最大 132.0 ms
#:     ★ 串成一条        中位 7.1 ms   p95  14.5 ms   最大  15.4 ms
#:     并行+switch 1ms   中位 14.3 ms  p95  53.8 ms   最大  85.8 ms
#:
#: 总耗时一模一样（2357 vs 2371 ms）—— 并行一点没赚到，只是把延迟放大了
#: 一个数量级。而发送队列排不干净的后果，用户 2026-09-01 23:59 看得清清楚楚：
#: 下行积压两秒，等预热线程一算完，5.6 KB 在 4 毫秒里一起涌进客户端
#: （bot 位置突变、空中的子弹突然全冒出来）。
_WARM_QUEUE = collections.deque()
_WARM_CV = threading.Condition()
#: `[线程, 正在算几份]` —— 和 `botplan.Planner` 同一套记账。
_WARM_WORKER = [None, 0]


def _warm_worker_loop():
    while True:
        with _WARM_CV:
            while not _WARM_QUEUE:
                _WARM_CV.wait()
            job = _WARM_QUEUE.popleft()
            _WARM_WORKER[1] += 1
        try:
            _warm_navigation_now(*job)
        except Exception as error:          # noqa: BLE001 —— 纯缓存，不许炸
            # ★ 一份炸了不能把这条线程带走：它死了之后**所有**图都不再预热，
            #   而预热失败本身一点行为都不影响（游戏线程该算就自己算）。
            asynclog.emit(f"[bot] ⚠ 可达图预热线程吞掉一个异常: {error!r}")
        finally:
            with _WARM_CV:
                _WARM_WORKER[1] -= 1
                _WARM_CV.notify_all()


def _submit_warm(job):
    """把一份预热活儿排进那条唯一的线程（线程懒启动，和 `botplan` 同款）。"""
    with _WARM_CV:
        _WARM_QUEUE.append(job)
        worker = _WARM_WORKER[0]
        if worker is None or not worker.is_alive():
            worker = _WARM_WORKER[0] = threading.Thread(
                target=_warm_worker_loop, name="botnav-warm", daemon=True)
            worker.start()
        _WARM_CV.notify()


def warm_settle(timeout=30.0):
    """等到预热队列排空、也没有正在算的。**给单测和收工检查用。**"""
    deadline = time.monotonic() + float(timeout)
    with _WARM_CV:
        while _WARM_QUEUE or _WARM_WORKER[1]:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            _WARM_CV.wait(left)
        return True


def _warm_navigation_now(terrain, who, seeds, label):
    try:
        started = time.monotonic()
        # ★ 弹道那张粗网格也在这儿一起烘（`_terrain_contact` 用它跳过空气）。
        #   整张图算一次要 20~90 ms —— 落在战斗那一格里就是一次可见的卡顿，
        #   而它和可达图一样是「地形的静态事实」，正该跟着一起预热。
        #   variant（破坏物碎了）不用重烘，它是从这一份补出来的，几十微秒。
        terrain.bullet_coarse()
        coarse_ms = (time.monotonic() - started) * 1000
        count = botnav.warm(terrain, who, seeds)
        asynclog.emit(f"[bot] 可达图预热完毕 {label}：{count} 个落脚点，"
                      f"{(time.monotonic() - started) * 1000:.0f} ms"
                      f"（其中弹道粗网格 {coarse_ms:.0f} ms）")
    except Exception as error:              # noqa: BLE001 —— 纯缓存，不许炸
        asynclog.emit(f"[bot] ⚠ 可达图预热失败 {label}: {error!r}")


def warm_navigation(room, why):
    """★★ 开局 / 换图时，**在后台**把这张图的可达图算出来（会话 41）。

    ## 为什么要预热

    `botnav` 的边缓存把「目标够不着」那一次泛洪从 270 ms 压到 4.6 ms，
    但**第一次**还是得老老实实算一遍（真图 340 ms ~ 1.7 秒）。战斗中现算
    的话，那一下正好落在真人的同步转发路径上 —— 就是用户看到的卡顿。

    ## 为什么可以放后台线程

    可达图是 **(地形, 角色尺度)** 的静态事实：地形只读，`botmove` 是纯函数，
    缓存写入幂等（谁先算完都一样，同时算也只是白做一遍功）。
    它**不驱动任何行为**，所以不违反 D17「不给 bot 另起定时器线程」——
    那一条禁的是「用定时器代替事件去推 bot 的帧」。

    预热还没跑完时，游戏线程该算就自己算，只是慢一点 —— 行为一模一样。

    ## 从哪儿开始泛洪

    地图自己的**出生点表**（`terrain.points`）。所有人都是从那儿进图的，
    从那儿走得到的地方就是这一局真正会用到的那一片。
    """
    terrain = _terrain(room)
    if terrain is None:
        return
    seeds_raw = [point for group in terrain.points.values() for point in group]
    if not seeds_raw:
        return
    variants = [terrain]
    if getattr(terrain, "breakables", ()) and getattr(terrain, "alive", frozenset()):
        opened = terrain.variant(())
        if opened is not terrain:
            variants.append(opened)
    for index in room.bot_seats():
        seat = room.seats[index]
        machine = None if seat is None else seat.conn
        if not isinstance(machine, BotConn):
            continue
        who = _character_of(machine)
        key = botnav._scale_key(who)
        for nav_terrain in variants:
            with _WARM_LOCK:
                done = _WARMED.setdefault(nav_terrain, set())
                if key in done:
                    continue
                done.add(key)
            seeds = []
            for point in seeds_raw:
                # ★ 走 `_settle_spawn()`（`on_ground=False` 起步、往下掉一趟）
                #   —— 出生点对象在编辑器里挂在半空（§91）。
                body = _settle_spawn(nav_terrain, machine, point)
                if body is not None and body.on_ground:
                    seeds.append(body)
            if not seeds:
                continue
            suffix = "无破坏物捷径" if nav_terrain is not terrain else "完整地形"
            label = (f"{nav_terrain.name} 角色{machine.character_id} "
                     f"{suffix}（{why}）")
            # ★★ 排进那条**唯一**的预热线程，不再一个尺度一条（§163）。
            _submit_warm((nav_terrain, who, seeds, label))


def _emit(machine, packet):
    """把一份合成好的 `UdpPacket` 交给现成的投递路（**不新增第二条**）。"""
    return machine.sync.deliver(packet, gameserver.PEER_RELAY.deliver)


def tick_room(room, tick, now, behind=0):
    """房里每个 bot 走**一格**（32 ms）。由 `gameserver.RoomLoop` 调（D106）。

    `tick` 是本局第几格，`now` 是这一格的**绝对时刻**（`t0 + tick × 32 ms`）——
    追赶时它是过去的时刻，物理照着它算才对得上。

    ★ 为什么不再挂在真人的同步包上（**废止 D17**）：收方对远端弹体是每
    32 ms 推一格、撞地形就本地自灭的，而真人的心跳约 128 ms 一发 ——
    跟着真人走，`rpExplode` 系统性地晚一个帧距、被静默丢弃，弹体句柄
    计数器从此永久错开（§147）。要抄的不是「原版服务端做了什么」
    （它只是中继，什么都不模拟），是「**原版射手做了什么**」。

    ★ 加载阶段**不走这里**：bot 的进度条是在广播 `0x0400` / `0x0417` 那一刻
    一次性报满的（`report_bots_loaded`，D26），不需要逐格驱动。

    ★ 抛出去的异常一律吞掉：一个 bot 出问题不能连累同房间别的 bot，
    也不能把房间那条循环带崩（D1）。
    """
    if room is None or not room.is_playing():
        return
    with _tick_clock(now):
        _tick_room_locked(room, tick, now, behind)


def _tick_room_locked(room, tick, now, behind=0):
    """`tick_room()` 的正文 —— 这一格的时刻已经装好了（`_tick_clock`）。"""
    # ★ 血量台账每格过一遍「躺着 -> 站起来」的翻转（M5-C）。放在最外层：
    #   它是**房间级**的事实，和某一个 bot 这一格动没动无关。
    try:
        _refresh_health(room)
    except Exception as error:          # noqa: BLE001 —— 见 docstring
        asynclog.emit(f"[{gameserver.ts()}] [bot] ⚠ 刷新血量台账出错，已跳过: {error!r}")
    # ★★★ 可破坏物碎了 / 长回来了（§138）—— 地形对象跟着换一份。
    try:
        _refresh_breakables(room)
    except Exception as error:          # noqa: BLE001 —— 见 docstring
        asynclog.emit(f"[{gameserver.ts()}] [bot] ⚠ 刷新破坏物出错，已跳过: {error!r}")
    # ★★★ **真人的身体先推一格**（D106）：这一格里 bot 判命中 / 瞄准用的
    #    「人在哪」就是它。排在所有 bot 前面 —— 同一格里每个 bot 看到的
    #    世界必须是同一个。
    try:
        _advance_humans(room, _terrain(room))
    except Exception as error:          # noqa: BLE001 —— 见 docstring
        asynclog.emit(f"[{gameserver.ts()}] [bot] ⚠ 外推真人位置出错，已跳过: "
                      f"{error!r}")
    for index in room.bot_seats():
        seat = room.seats[index]
        machine = None if seat is None else seat.conn
        if not isinstance(machine, BotConn):
            continue
        try:
            _tick_bot(room, machine, index, tick, now, behind)
        except Exception as error:          # noqa: BLE001 —— 见 docstring
            # ★ 连 `sync` 本身都坏了的场合（单测里就是这么造的）也要接住 ——
            #   这一层的全部意义就是「一个 bot 坏掉不许连累别人」。
            try:
                machine.sync.broken = True
            except Exception:               # noqa: BLE001
                pass
            machine.log(f"   ⚠ bot 帧出错，已停掉它的同步: {error!r}")


#: ★ 把驱动挂进 `gameserver`（§14 的导入方向：`gameserver` 不许 import 本模块）。
#:
#: `app.py` 启动时有一发显式 `import bot`，所以真服务端里这个钩子一定装上；
#: 单测里 `import bot` 同样会装。没装（有人只 import 了 `gameserver`）时
#: `_relay_battle_tick` 那边是 `None` 判空，行为退回「房里没有 bot」。
gameserver.BOT_ROOM_TICK = tick_room
#: 同上：`0x0400` / `0x0417` 广播出去之后，bot 的进度条一次性报满（D26）。
gameserver.BOT_ROOM_LOADED = report_bots_loaded
#: 同上：bot 该在哪重生（§91）。看门狗补 `0x0419` 之前问这一发。
gameserver.BOT_RESPAWN_POINT = pick_respawn_point
#: 同上：真人打出来的每一发同步包都过一次，打到 bot 身上的替它结算击退（§92）。
gameserver.BOT_PEER_HIT = note_peer_hit


def handle_command(conn, text):
    """聊天里的 bot 命令层。返回 ``True`` = 已消费（这行聊天不再广播）。

    ★ **非房主敲的命令不吞**（PLAN M1）：原样当聊天广播出去，别人才看得见
    他说了什么。但同时**私下**回他一行「只有房主能用」—— 否则他敲完什么都
    没发生，只会以为服务端坏了。两件事不矛盾：广播照旧，多一行提示。
    """
    parsed = parse_command(text)
    if parsed is None:
        return False
    name, args = parsed
    if name not in COMMANDS:
        return False                       # `/` 开头的普通聊天，原样放行

    room = conn.lobby_room()
    if room is None:
        conn.send_system_chat("bot 命令只能在房间里用。")
        return True
    seat_index = room.seat_index_of(conn)
    if seat_index != room.host_seat:
        # ★ 不消费：这行照常当聊天广播出去。
        conn.send_system_chat("bot 命令只有房主能用。")
        return False
    if name in MUTATING_COMMANDS and room.is_playing():
        conn.send_system_chat("游戏进行中改不了 bot，等这一局打完再说。")
        return True

    try:
        warning = COMMANDS[name](conn, room, args)
    except Exception as error:             # noqa: BLE001 —— 见下
        # ★ bot 命令是**聊天线程**上跑的：抛出去会把房主自己的连接带崩
        #   （`on_game_packet` 之上没有兜底）。一条命令写错不该让人掉线。
        conn.log(f"   ⚠ bot 命令 /{name} {args} 出错: {error!r}")
        conn.send_system_chat(f"命令 /{name} 出错了，看服务端日志。")
        return True
    if warning:
        conn.send_system_chat(warning)
    return True
