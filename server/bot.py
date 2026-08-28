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
import struct
import threading
import time

from account_store import BASE_CHARACTER_IDS, MINIMUM_PLAYER_LEVEL, \
    PREMIUM_CHARACTER_IDS
import ballistics
import botmove
import botsync
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
        self.batch_delay_ms = 0
        self.connected_at = time.monotonic()
        # -- 只属于 bot 的机器状态（D9）--------------------------------------
        #: 这个 bot 的同步流（M3）：序号记账 + 组包，见 `botsync.py`。
        self.sync = botsync.BotSyncStream(self)
        #: 战斗中的落脚点 `(x, y)`，None = 还不知道自己站在哪。
        #: ★ 对战房里第一帧从**地图出生点**取（§91，和真人同一套分配规则）；
        #:   闯关房没有出生点可用时才退回「跟着真人走」那条老路（D16）。
        self.battle_pos = None
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
        #: 上一次推运动状态的时刻（`time.monotonic()`）。按**真实流逝的
        #: 时间**决定推几个 tick，和 `_advance_shells()` 一个口径。
        self.move_at = 0.0
        #: ★ 临时诊断（会话 17）：上一次打过的「为什么不开枪」，用来做状态翻转
        #:   去重。跟 `BOT_DIAG_FIRE_ANYWHERE` 一起删。
        self.diag_last_why = ""
        #: 现在朝哪走：`+1` 右 / `-1` 左。
        self.heading = botsync.FACING_RIGHT
        #: ★ 上一帧消费到的是**谁的第几个位置点**：`(座位号, sync_trail_seq)`。
        #:   bot 的帧就是靠它对齐的 —— 号变了 = 真人报了新位置 = 走一帧
        #:   （V0.3 §32，代替了原来那个 0.125 秒的采样率限流）。
        self.last_trail_mark = None
        #: 这一轮加载报过那一发 `0x4005` 了吗（D26）。
        #: `None` = 还没报。报过之后恒为 100，拿它**按状态翻转去重**。
        self.load_progress = None
        #: ★ 现在蹲着没有（§41）。蹲**不在心跳里**，只有 `rpCrouch` 那一发
        #:   事件包说得着，所以这边得自己记着状态、**只在翻转时发**。
        self.crouched = False
        #: 下一发子弹最早什么时候能打（`time.monotonic()` 的刻度）。
        #: ★ 这是**唯一**一个时间阈值，值来自 `weapon.ini` 的 `CoolingTime`
        #:   —— 原版这把枪就是这个节奏，不是我拿一台机器观测出来的常量
        #:   （铁律 10 / D29）。
        self.next_fire_at = 0.0
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
        #: 房主用 `/w N M` 指定的武器**槽位**（1/2/3）；`None` = 用首选。
        #: ★ **不跟着换图清** —— 那是房主给的指令，不是一图之内的机器状态。
        self.weapon_slot = None
        #: 房主用 `/s N` 让它站住了吗。★ 同上，跨图保持。
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
        # ★ 自己的运动状态跟着清：新一张图的地形、出生点全变了，
        #   上一张图的落脚点和速度一个字都不作数（同 `battle_pos`）。
        self.body = None
        self.on_ground = None
        # ★ 待定的重生点跟着清：新一张图的出生点表整个换了一份（§91）。
        self.pending_spawn = None
        self.move_at = 0.0
        self.last_trail_mark = None
        self.load_progress = None
        self.crouched = False
        self.sync.reset_projectiles()
        # ★ 在飞的子弹一起丢：收方的弹体表和句柄计数器这一刻也整个复位
        #   （`ForceReloadTerrain`），上一张图的弹体在那边已经不存在了 ——
        #   补发只会拿一个查不到的句柄去撞 `0x492750` 那个静默丢弃。
        self.pending_shots = []
        # ★ 火墙跟着清：收方的弹体表这一刻整个复位，上一张图那几团火
        #   在那边已经不存在了（同 `pending_shots`）。
        self.fires = []
        # ★ 火烧的免伤时刻戳跟着清：换图 / 新一局客户端把角色重建，
        #   `[角色+0x160]` 也跟着归零（同 `crouched` 的道理，§85）。
        self.burnt = {}
        self.next_fire_at = 0.0
        self.rounds_left = None
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

        ★ 房主用 `/w N M` 指定过槽位就用那一把；那一把不可用（或者换角色
        之后那个槽位空了）就**退回首选**，不是不开火 —— 房主的指令是
        「用几号枪」，不是「打不了就别打」。
        """
        if self.weapon_slot is not None:
            chosen = weapondata.slot_for(self.character_id, self.weapon_slot)
            if chosen is not None:
                return chosen
        return weapondata.preferred_for(self.character_id)

    def __repr__(self):
        return f"<BotConn {self.nickname} 座位 {self.my_seat}>"

    def is_bot_conn(self):
        """★ `gameserver` 认 bot 的**唯一**入口（它 import 不了本模块，§14）。"""
        return True

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
        print(f"[{gameserver.ts()}] {self.nickname} {msg}", flush=True)

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
    "bot 命令（房主专用，N = 座位号，/h 重看）：",
    "/a [n] 加 n 个（默认 1）;  /r 全部准备（再敲取消）",
    "/c N M 换角色（M=1~3）;  /t N 换队（组队战）",
)

#: ★ **战斗中**的 `/h`。房间里那几条会被 `MUTATING_COMMANDS` 挡掉，列出来
#: 只会占满那 4 行的额度（§20），所以这里只放战斗中真能用的两条。
BATTLE_HELP_LINES = (
    "战斗中：/s N 让 bot 站住;  再敲一次恢复跟随",
    "/w N M 换武器（M=1~3）;  /dash 近身攻击开关",
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


#: 命令名 -> 处理函数。`/h` 和 `/?` 是 `/help` 的别名。
#: 在这张表里 = 「这是一条 bot 命令」，命令层据此决定要不要吞掉这行聊天。
#:
#: ★ `team` 还在表里，但它**不是**换队命令，只是一行「请改用 /t」的提示
#: —— 客户端自己把 `/team ` 当队伍聊天吃掉了，服务端根本收不到（§19）。
COMMAND_NAMES = ("a", "c", "t", "team", "r", "help", "h", "?")


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


def _cmd_hold(conn, room, args):
    """`/s [N]` —— 让 bot **站在原地不动**（再敲一次恢复跟随）。

    ★ 这是**测试手段**，不是玩法：bot 平时走的是真人趟过的路（D16），
    所以「隔着墙打不打得到」这种事在实机上根本碰不上 —— 中间不可能有墙。
    站住之后房主可以自己走开、绕到地形后面，才验得了 `line_blocked()`
    那条判据（用户 2026-08-26 要的）。

    ★ 战斗中**必须能用**（那正是要用它的时候），所以它不在
    `MUTATING_COMMANDS` 里。
    """
    seats, error = _battle_bots(room, args)
    if error:
        return f"/s 用法：/s [座位号]（不给就是全部）。{error}"
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
    conn.log(f"   /s: {changed}")
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


def _cmd_gun(conn, room, args):
    """`/w N [M]` —— 给 bot 换武器（M = 槽位 1/2/3）；不给 M 就列出有哪些。

    ★★ **为什么只有手动换，没有自动换**：「什么时候该换枪」是 AI 决策，归 M5。
    换枪这条链（`rpChangeWeapon` + 句柄步进跟着变）本身现在就能实机验。

    ★ 只列 / 只接受 `weapondata` 认可的武器（句柄步进算得出 + 有伤害 +
    初速模式认得 + 弹体类服务端有飞行模型）：步进猜错的表现是「子弹飞过去
    不炸」，而且静默、一局之内不自愈（§42）。

    ★ 会话 21（§72）之后 16 个玩家角色里**10 个三个槽位全可用**；剩下
    6 个各缺一个槽：106 / 110 的 2 号是**反弹弹**（会弹墙）、107 / 108 的
    2 号是**炮台**、109 的 3 号是**等离子炮**（那几类的飞行服务端还没有
    模型），角色 3 的 3 号是图腾发射器（`Damage=0`，打不动人）。
    """
    index, error = _seat_arg(args)
    if error:
        return f"/w 用法：/w <座位号> [武器槽 1~3]。{error}"
    seat, error = _bot_seat(room, index)
    if error:
        return f"换不了武器：{error}"
    machine = seat.conn
    if not isinstance(machine, BotConn):
        return f"换不了武器：{index} 号位上不是 bot。"
    choices = weapondata.usable_for(machine.character_id)
    if not choices:
        return (f"{seat.nickname} 这个角色一把能用的武器都没有，它只跑不打。")
    if len(args) < 2:
        listed = "；".join(
            f"{w.raw['slot']}={w.damage}伤{'抛' if w.gravity else '直'}"
            + ("（当前）" if machine.weapon is not None
               and w.id == machine.weapon.id else "")
            for w in choices)
        return f"{seat.nickname} 可用武器槽：{listed}。敲 /w {index} <槽位> 换。"
    try:
        slot = int(str(args[1]).strip())
    except (TypeError, ValueError):
        return f"武器槽 {args[1]!r} 不是数字。"
    chosen = weapondata.slot_for(machine.character_id, slot)
    if chosen is None:
        ok = "、".join(str(w.raw["slot"]) for w in choices)
        return f"{seat.nickname} 没有能用的 {slot} 号武器槽。可选：{ok}。"
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
    conn.room_system_chat(
        f"{seat.nickname} 换成了 {slot} 号武器（{chosen.damage} 点伤害）。")
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
    "s": _cmd_hold,
    "w": _cmd_gun,
    "dash": _cmd_dash,
    "noboom": _cmd_noboom,
    "slow": _cmd_slow,
    "help": _cmd_help,
    "h": _cmd_help,
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
    print("[bot] ★★ BOT_DIAG_FIRE_ANYWHERE 已开 —— bot 会无视交战距离和地形"
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
    """房间**这一刻**在哪张图上。闯关换过图的话就是最后进的那张。"""
    quest = room.quest
    entered = getattr(quest, "maps_entered", None) if quest is not None else None
    if entered:
        return entered[-1]
    return room.map_name


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


def _direct_hit_damage(room, machine, weapon, region, victim_seat):
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
    damage = weapon.damage_for(region) * _damage_scale(room)
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


class PeerShot(object):
    """真人打出去的一发（`rpFire`），留着给击退算来向用（§92）。"""

    __slots__ = ("weapon", "x", "y", "shot")

    def __init__(self, weapon, x, y, shot):
        self.weapon = weapon
        self.x = float(x)
        self.y = float(y)
        self.shot = shot


def note_peer_fire(conn, body):
    """记一发真人的 `rpFire`（§92）。解不出武器就不记。"""
    if len(body) < botsync.FIRE_BODY_SIZE:
        return
    ammo, fx, fy, angle, power = struct.unpack_from("<iffff", body, 2)
    weapon = weapondata.get(ammo)
    if weapon is None:
        return
    shots = getattr(conn, "peer_shots", None)
    if not isinstance(shots, collections.deque):
        shots = conn.peer_shots = collections.deque(
            maxlen=BOT_PEER_SHOT_KEEP)
    shots.append(PeerShot(weapon, fx, fy,
                          ballistics.launch(weapon, angle, power)))


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
            best = (error, vx, vy + shot.gravity * ticks)
    if best is None or best[0] > BOT_PEER_SHOT_TOLERANCE:
        return None
    return (best[1], best[2])


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
        note_peer_fire(conn, payload[udpsync.PEER_HEADER_SIZE:])
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
    terrain = mapdata.load(_current_map(room))
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
    terrain = mapdata.load(_current_map(room))
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
    trail = getattr(conn, "sync_trail", None)
    if not trail:
        return None
    point = trail[-1]
    return (point[0], point[1], bool(point[7]) if len(point) > 7 else False)


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
    trail = getattr(conn, "sync_trail", None)
    if not trail:
        return None
    point = trail[-1]
    return bool(point[3]) if len(point) > 3 else None


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
    """
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
BOT_MOVE_MAX_TICKS = botmove.TICKS_PER_BEAT * 4


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
        terrain = mapdata.load(_current_map(room))
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
    terrain = mapdata.load(_current_map(room))
    point = respawn_point(room, seat_index, terrain, roll=machine.roll)
    if point is None:
        return None
    if terrain is not None:
        body = _settle_spawn(terrain, machine, point)
        point = (body.x, body.y)
    machine.pending_spawn = point
    return point


def _enemy_spot(room, machine, seat_index):
    """离自己最近的那个敌人此刻站在哪；没有敌人返回 `None`。

    ★ 闯关房恒为 `None`（`_hostile_targets` 那边就是空的）—— 那儿该打的是
    怪，而怪的位置服务端手里没有，所以闯关仍然回放真人轨迹「跟着推进」。
    """
    if machine.body is None:
        return None
    best = None
    for _index, tx, ty, _crouched in _hostile_targets(room, seat_index):
        span = abs(tx - machine.body.x)
        if best is None or span < best[0]:
            best = (span, (float(tx), float(ty)))
    return None if best is None else best[1]


def _move_intent(room, machine, seat_index, terrain, target):
    """这一帧往哪走 —— 返回 `(方向, 要不要起跳)`。

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

    ⚠ 还没有的：绕路（真正的可达性搜索）、提前量、冲刺跑、蹲。
    """
    body = machine.body
    if body is None or terrain is None:
        return (0, False)
    if target is not None:
        return (0, False)
    spot = _enemy_spot(room, machine, seat_index)
    if spot is None:
        return (0, False)
    direction = 1 if spot[0] >= body.x else -1
    who = _character_of(machine)
    if not body.on_ground:
        # ★★ 腾空时方向键**改不了**水平速度（§93 推翻了 §71 的那一行）——
        #   这里照样返回方向，只是为了这一批 tick 里**落地之后**那几个 tick
        #   接着往前走；空中那几个 tick 它是死的。
        return (direction, False)
    if botmove.blocked(terrain, body, who, direction):
        return (direction, True)
    if botmove.drop_below(terrain, body, who, direction) is None:
        # 脚下这一步是个掉不到底的坑：跳得过去就跳，跳不过去就别走。
        landing = botmove.jump_lands(terrain, body, who, direction)
        return (direction, True) if landing is not None else (0, False)
    return (direction, False)


def _own_step(room, machine, seat_index, terrain, target, now):
    """自己走一帧，返回和 `trail_point()` 同格式的那个八元组；
    还接管不了就返回 `None`（调用方退回回放真人轨迹）。

    接管不了的情形只剩两种，都退回 D16 那条老路：

    1. **没有地形数据**（这张图没提取到）—— 没有地面就没法自己走；
    2. **闯关房** —— 那儿的走位是「跟着真人推进」，不是自己找敌人。

    ## ★★★ 「场上没有敌人」**不再**退回真人轨迹（§91）

    这里原来的第一句是 `if not _hostile_targets(): return None`。
    真人一死，`_lying_dead()` 把他从敌人表里剔掉 ⇒ 敌人表空 ⇒ bot 当场
    退回 `trail_point(真人轨迹)`，**一瞬间被拽到真人身边**，然后每一帧
    在「自己算的点」和「真人轨迹上的点」之间来回跳 —— 用户 2026-08-28 报的
    「我被打死的一瞬间 bot 瞬移到我身边然后不停抽搐」就是这一句。

    没有敌人是**很正常的一刻**（对面全躺着等重生），这时候 bot 该做的是
    站在原地 —— `_move_intent()` 本来就会返回 `(0, False)`。
    """
    if terrain is None:
        return None
    if room.team_layout() == lobby_module.TEAM_LAYOUT_COOP:
        return None
    who = _character_of(machine)
    if machine.body is None:
        # ★★★ 第一帧的锚是**这个座位该用的地图出生点**（§91）——
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
        machine.move_at = now
    if _may_walk(machine, now):
        direction, want_jump = _move_intent(room, machine, seat_index,
                                            terrain, target)
    else:
        # ★★ 进图那两秒站着不动（§94）—— 屏幕上还在放「预备 / 开始」，
        #   真人这时候也动不了。**心跳照发**（站着的姿势），只是不迈腿；
        #   被顶飞的话下面那几个 tick 照样把它推出去（`direction` 在空中
        #   本来就不起作用，§93）。
        direction, want_jump = 0, False
    # ★★ 推几个 tick 按**真实流逝的时间**算（和 `_advance_shells()` 一个
    #   口径），而且**余数留到下一帧**：一发心跳 125 ms = 3.9 个 tick，
    #   每帧直接截断的话 bot 会稳定地比真人慢 23% —— 攒起来就不会。
    ticks = int(max(0.0, now - machine.move_at) * botmove.TICKS_PER_SECOND)
    if ticks > BOT_MOVE_MAX_TICKS:
        ticks = BOT_MOVE_MAX_TICKS
        machine.move_at = now          # 攒得太久（读图 / 断流）—— 丢掉，别瞬移
    else:
        machine.move_at += ticks * botmove.TICK_MS / 1000.0
    before = machine.body
    machine.body = botmove.advance(terrain, before, who, ticks,
                                   direction=direction, want_jump=want_jump)
    body = machine.body
    jumped = 1 if (want_jump and before.on_ground and not body.on_ground) else 0
    return (body.x, body.y, jumped, body.on_ground, body.vx, body.vy,
            False, False)


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
    if radius and radius >= 1.0:
        if _disc_blocked(terrain, int(x0), int(y0), _disc_offsets(radius)):
            return True
    elif terrain.blocks_bullet(int(x0), int(y0)):
        return True
    points = ballistics.path_points(x0, y0, shot)
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        if terrain.line_blocked(ax, ay, bx, by, step=BOT_LINE_STEP):
            return True
    return False


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


def _fire_target(room, machine, seat_index, weapon):
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
    if machine.battle_pos is None:
        return None
    x, y = machine.battle_pos
    terrain = mapdata.load(_current_map(room))
    best = None
    for index, px, py, crouched in _hostile_targets(room, seat_index):
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
        if best is not None and span >= best[0]:
            continue                       # 已经有更近的了，弹道就别解了
        shot = ballistics.solve(weapon, tx - mx, ty - my,
                                speed=_lob_speed(weapon, tx - mx, ty - my))
        if shot is None:
            continue
        if _outlives_fuse(weapon, shot):
            continue
        if (not BOT_DIAG_FIRE_ANYWHERE
                and _path_blocked(terrain, mx, my, shot, weapon.size)):
            continue
        best = (span, index, (tx, ty), shot)
    if best is None:
        return None
    shot = best[3]
    if machine.slow_bullet:
        shot = _slow_shot(weapon, shot)
    return (best[1], best[2], shot)


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
    现在换枪的唯一来源是房主的 `/w N M`。
    """
    if machine.declared_weapon == weapon.id:
        return False
    machine.declared_weapon = weapon.id
    # ★ 换枪 = 换弹匣：新武器从满弹匣开始（真人切枪也是这样）。
    machine.rounds_left = None
    _emit(machine, machine.sync.event(
        botsync.OP_CHANGE_WEAPON,
        botsync.change_weapon_body(seat_index, weapon.id)))
    return True


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
        hostiles = list(_hostile_targets(room, seat_index))
        if not hostiles:
            why = "没有敌人（队伍分边？位置还不知道？）"
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
                 "shot", "born", "ticks", "x", "y", "max_ticks",
                 "vx", "vy", "locked", "bounced")

    def __init__(self, handle, fire_seq, weapon, group, x0, y0, shot, born,
                 max_ticks):
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


#: 圆盘 / 圆环的整数格偏移表，按半径缓存（半径都是 `weapon.size`，就那几种）。
_DISC_OFFSETS = {}
_RIM_OFFSETS = {}


def _disc_offsets(radius):
    """半径 `radius` 的**实心**圆盘覆盖到的整数格偏移。"""
    key = int(radius)
    got = _DISC_OFFSETS.get(key)
    if got is None:
        limit = key * key
        got = tuple((ox, oy)
                    for oy in range(-key, key + 1)
                    for ox in range(-key, key + 1)
                    if ox * ox + oy * oy <= limit)
        _DISC_OFFSETS[key] = got
    return got


def _rim_offsets(radius):
    """半径 `radius` 的圆盘**最外一圈**（厚 1 格）的整数格偏移。

    ★ 沿线一格一格走的时候只查这一圈就够：圆盘每步只挪 1 格，
    任何格子要落进圆盘内部，**必先经过某一步的外圈**。
    起点那一格例外（弹体可能直接生在地形里），所以起点查整个圆盘。
    """
    key = int(radius)
    got = _RIM_OFFSETS.get(key)
    if got is None:
        outer = key * key
        inner = (key - 1) * (key - 1)
        got = tuple((ox, oy)
                    for oy in range(-key, key + 1)
                    for ox in range(-key, key + 1)
                    if inner < ox * ox + oy * oy <= outer)
        _RIM_OFFSETS[key] = got
    return got


def _disc_blocked(terrain, x, y, offsets):
    """圆心在 `(x, y)` 的那个圆盘（用给定偏移表）碰到挡子弹的格子了吗。

    ★★ **图顶上面（`y < 0`）不算**（§83）。`TerrainData::Get`（`0x472fe0`）
    对出界一律返回 2，可实机逐帧日志里弹体是**从图顶飞出去又落回来**的
    （句柄 200048 第 36 帧在 `(1323.48, 0.68)`，圆盘早该顶到 `y = −7` 了，
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

    ## ★★★ 弹体是**有粗细**的，不是一个点（§83）

    这里原来只查**圆心**那一格。可弹体的碰撞形状是半径 `Size` 的圆盘
    （`weapon.ini` 的 `Size`：直射弹 4、抛射弹 8），收方是拿**整个形状**
    去撞地形的 —— 于是「圆心还在空中、边缘已经蹭上平台」的那些发，
    服务端全部放行、一路飞下去，在几百个单位以外才炸。

    实机对上的证据（2026-08-28 的客户端逐帧日志 `PROJ.`，Forest_b）：
    6 发被地形停住的火焰弹，**停下那一点到最近的实心格的距离分别是
    8.06 / 7.00 / 6.71 / 8.94 / 7.00 / 6.40** —— 全部贴着 `Size = 8`，
    而不是 0~1.4（那才是「只看圆心」该有的样子）。
    其中句柄 200072 那一发停在 `(1070, 369)`，服务端却让它又飞了 1.1 秒、
    在 `(1644.8, 582.3)` 才炸 —— 用户报的「手雷空中飞一半突然消失了，
    过了一会儿在右边地图边缘出现了爆炸动画」就是它。

    ⚠ 反弹要用**撞上之前**那一点（收方也是把弹体夹回最后一个不重叠的
    采样点），否则弹体贴在地形里，下一 tick 一开头又撞上，原地卡死。
    """
    if terrain is None:
        return (None, None)
    if radius and radius >= 1.0:
        rim = _rim_offsets(radius)
        if _disc_blocked(terrain, int(ax), int(ay), _disc_offsets(radius)):
            return (0.0, 0.0)
        probe = lambda x, y: _disc_blocked(terrain, x, y, rim)   # noqa: E731
    else:
        if terrain.blocks_bullet(int(ax), int(ay)):
            return (0.0, 0.0)
        probe = terrain.blocks_bullet
    dx = bx - ax
    dy = by - ay
    span = max(abs(dx), abs(dy))
    steps = max(1, int(span // BOT_SHELL_TERRAIN_STEP))
    for i in range(1, steps + 1):
        t = float(i) / steps
        if probe(int(ax + dx * t), int(ay + dy * t)):
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


def _bounces_off_terrain(weapon):
    """这把武器的弹体撞了地形是**弹开**还是**当场炸**（§84）。

    判据是**有没有引信**（`SliceTime`）：有引信的弹体（`AppleGrenade` /
    `SeedBomb` / `SliceBullet`）到期在每一台机器上自爆（§72），地形拦不住
    它 —— 语料 2353 发苹果雷里「没打中角色」的那些**爆炸时刻死死压在
    11~13 发心跳上**（p50 = 12 ≈ `SliceTime` 1500 ms / 128 ms），
    要是撞地就炸，这个分布该是散开的。

    用户 2026-08-28：「真人对局时，苹果弹直接扔到地上会弹跳，
    过一会儿之后才会炸开。」

    ★ 反过来，火焰弹（`FlamingBottle`，没有 `SliceTime`）语料里落空的
    p50 只有 6 发心跳、分布很散 —— 那就是「撞上什么就炸在那儿」。
    """
    return bool(weapon.fuse_ticks)


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


#: 量地形法线时往外看多远 —— 弹体半径的**两倍**（§88）。
#:
#: ★ 这是个**拟合值**，不是逆出来的：原版怎么算法线还没读出来，
#: 只能拿实机的 57 次反弹（客户端逐帧日志，Forest_b）反推每一次的法线，
#: 再看哪个采样半径最贴。出射方向的中位误差：
#:
#:     半径  9（= Size+1，会话 25 用的）  17.1°   ★ 还有 27 次根本量不出法线
#:     半径 12                            10.2°
#:     半径 16（= Size×2）                10.4°   p75 15.4°  p90 23.5°
#:     半径 20                            10.8°
#:
#: 12~20 之间是平的，取 `Size × 2` 只是因为它是个跟着武器走的比例，
#: 不是又一个魔数。⚠ 残差写在 §88 里，别把它当成对的。
BOUNCE_NORMAL_REACH = 2.0


def _terrain_normal(terrain, x, y, radius):
    """`(x, y)` 附近那片地形的法线（指向弹体这一侧），量不出来返回 `None`。

    做法：把 `BOUNCE_NORMAL_REACH × radius` 之内所有挡子弹的格子拿出来，
    每个格子投一票「从它指向圆心」的单位矢量，加起来再归一化。
    竖直的墙 / 水平的地面正好得到 `(±1, 0)` / `(0, ±1)`；斜坡得到坡的法线。

    ★ 图顶上面（`y < 0`）和 `_disc_blocked()` 一个口径，不算实心（§83）。

    ⚠ **近似**（§88）：原版怎么算法线没逆出来。能对死的只有图边那一类
    竖直面 —— 客户端实测的三发（例如 `v=(-26.76, -0.45)` → `(13.38, -0.23)`）
    正好是「x 取反、y 不动」，这个模型给的就是它；斜坡上中位差 10°。
    """
    cx, cy = int(x), int(y)
    nx = ny = 0.0
    for ox, oy in _disc_offsets(max(1.0, radius) * BOUNCE_NORMAL_REACH):
        yy = cy + oy
        if yy < 0 or not terrain.blocks_bullet(cx + ox, yy):
            continue
        span = math.hypot(ox, oy)
        if span <= 0.0:
            continue
        nx -= ox / span
        ny -= oy / span
    span = math.hypot(nx, ny)
    if span <= 1e-6:
        return None
    return (nx / span, ny / span)


def _bounce_shell(shell, terrain, ax, ay, bx, by, free_t):
    """弹体撞地形之后**弹开**（§84）：夹回撞上之前那一点、反射、速度减半。

    夹回去这一步不能省：贴在地形里的话下一 tick 一开头又撞上，
    速度一路对折，弹体原地卡死（§76 里客户端那个「速度每帧对折、
    位置一动不动」就是这个样子）。
    """
    px = ax + (bx - ax) * free_t
    py = ay + (by - ay) * free_t
    vx, vy = _shell_velocity(shell)
    normal = _terrain_normal(terrain, ax + (bx - ax), ay + (by - ay),
                             shell.weapon.size)
    if normal is None:
        normal = _terrain_normal(terrain, px, py, shell.weapon.size)
    if normal is not None:
        dot = vx * normal[0] + vy * normal[1]
        if dot < 0.0:              # 只有迎着面撞上去才反射
            vx -= 2.0 * dot * normal[0]
            vy -= 2.0 * dot * normal[1]
    shell.vx = vx * BOUNCE_RESTITUTION
    shell.vy = vy * BOUNCE_RESTITUTION
    shell.x, shell.y = px, py
    shell.bounced = True


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
    best_t = None
    best = None
    radius = shell.weapon.size
    for seat_index, px, py, crouched, character_id in bodies:
        character = chrprops.get(character_id)
        for cx, cy, r, region in character.circles(px, py, crouched):
            t = _segment_circle_t(ax, ay, bx, by, cx, cy, r + radius)
            if t is None or (best_t is not None and t >= best_t):
                continue
            best_t = t
            best = (seat_index, region)
    ground_t, free_t = _terrain_contact(terrain, ax, ay, bx, by, radius)
    if ground_t is not None and (best_t is None or ground_t < best_t):
        if _bounces_off_terrain(shell.weapon):
            _bounce_shell(shell, terrain, ax, ay, bx, by, free_t)
            return None
        best_t = ground_t
        best = (None, None)
    shell.x, shell.y = bx, by
    if best is None:
        return None
    point = (ax + (bx - ax) * best_t, ay + (by - ay) * best_t)
    shell.x, shell.y = point
    return (point, best[0], best[1])


#: ★★ **角色在溅射判定里的半径**：所有角色都是 **35**，写死在
#: `Character` 虚表槽 `+0x7c`（`0x4fc229: fld [0x693758]`）。
#:
#: 它被加进溅射的**作用半径**里 —— 也就是说 `SplashRange` 量的是「爆点到
#: 身体表面」，不是「爆点到身体中心」。
SPLASH_BODY_RADIUS = 35.0


def _splash_targets(room, shell, point, victim_seat, bodies):
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

    ★ 直接命中的那个人**不重复算**（他已经吃过 `rpExplode` 那一档伤害了）。

    ⚠ 这里**没有**夺分那两条 ×0.75（§89）—— 它们在 `0x47e618` 里，
    只有直接命中那条路会过。
    """
    weapon = shell.weapon
    span_max = float(weapon.splash_range or 0.0)
    if span_max <= 0.0:
        return []
    # ★ 目标那 35 是加在**半径**上的（`0x485831` 把两边的 `vft+0x7c` 相加）。
    reach = span_max + SPLASH_BODY_RADIUS
    full = int(weapon.splash_damage)
    scale = _damage_scale(room)
    out = []
    for seat_index, px, py, crouched, character_id in bodies:
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


def _resolve_shell(room, machine, shell, point, victim_seat, region):
    """这颗子弹到头了：发 `rpExplode`（+ 溅射的 `rpSplashDamaged`）。

    `victim_seat is None` = 打在地形上 / 飞出图外 —— 照样要发，
    **句柄记账不许漏**（§42）。
    """
    if machine.no_explode:
        # ★ 诊断开关（`/noboom`）：到头了也不发爆炸，让弹体一直飞下去。
        #   记录照样出队 —— 收方没收到爆炸就不会创建溅射对象，
        #   这边跟着一个都不记（§86），两边天然一致。
        return
    weapon = shell.weapon
    hit = victim_seat is not None
    # ★★ 夺分模式伤害翻倍（§87）+ 夺分独有的两条 ×0.75（§89）——
    #   原版是射手那台机器在把数字塞进包之前做的
    #   （`0x4806f1: shl` / `0x47e6df` / `0x47e6fe`）。
    damage = (_direct_hit_damage(room, machine, weapon, region, victim_seat)
              if hit else 0)
    # ★★★ 组包 + **爆炸对象的句柄记账**在同一次加锁里（§86）：收方处理这一
    #   发时会创建那个溅射对象，它和弹体共用同一个计数器。
    packet = machine.sync.explode(
        shell.handle,
        botsync.character_handle(victim_seat) if hit else 0,
        point[0], point[1],
        hit_kind=(botsync.HIT_CHARACTER if hit else botsync.HIT_NONE),
        damage=damage, spawns=weapon.explode_step)
    # ★ 诊断：命中 / 落空**各打一行**（按状态翻转去重，铁律 10）。M3b 收口后删。
    if hit not in machine.explode_logged:
        machine.explode_logged.add(hit)
        head, body = packet[:12], packet[12:]
        machine.log(f"   爆炸: 弹体句柄 {shell.handle} "
                    f"目标 {'座位%d 的%s' % (victim_seat, region) if hit else '落空'} "
                    f"爆炸点 ({point[0]:.1f}, {point[1]:.1f}) 伤害 {damage}"
                    f"　飞了 {shell.ticks} tick"
                    f"；头 {head.hex()} body({len(body)}) {body.hex()}")
    _emit(machine, packet)
    # ★★ 直接命中的击退**不进包**：每台机器都拿它自己那颗弹体的速度现算
    #   （`0x49285c` 读 `[proj+0x120]`，§92）。所以只有「被打的是另一个
    #   bot」时服务端才要自己补一份 —— 真人那份归他自己那台机器。
    if hit:
        vx, vy = _shell_velocity(shell)
        _knock_back_seat(room, victim_seat, damage,
                         knockback_vector(vx, vy, damage),
                         source="bot 直接命中")
    # ★★★ 溅射的名单和弹体**不是同一份**（§69）：弹体按碰撞排除组过滤
    #   （队友撞不着），而溅射对象的组恒为 0 = **撞所有人** ——
    #   队友、连射手自己都吃。所以这里重新问一次、不带组。
    for seat_index, splash, where, push in _splash_targets(
            room, shell, point, victim_seat,
            _battle_bodies(room, machine.my_seat, include_self=True)):
        # ★ 溅射伤害得**单独报**：收方处理 `rpExplode` 时确实会建一个
        #   `SplashDamage` 对象（§54 那个多出来的句柄），但算伤害的是射手
        #   那台机器 —— bot 没有本机，不补这一发就一滴血都不掉（§67）。
        # ★★ `+13/+17` 是**击退矢量**（§92）：不填的话被溅到的人一动不动，
        #   而真人扔的同一颗手雷会把人顶飞 —— 用户 2026-08-28 报的就是这个。
        _emit(machine, machine.sync.event(
            botsync.OP_SPLASH_DAMAGED,
            botsync.splash_body(shell.handle,
                                botsync.character_handle(seat_index),
                                splash, where[0], where[1],
                                push_x=push[0], push_y=push[1])))
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
        _set_ground_on_fire(room, machine, weapon, point)
    _split_shell(room, machine, shell, point, victim_seat)


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
    bodies = _battle_bodies(room, machine.my_seat, include_self=True)
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
                character = chrprops.get(character_id)
                lit = _fire_touch(character, px, py, crouched, wall.flames,
                                  radius, local)
                if lit is None:
                    continue
                machine.burnt[seat_index] = tick
                wall.burnt[seat_index] = local
                machine.log(f"   火烧: 座位{seat_index} 在 "
                            f"({lit.x:.0f}, {lit.y:.0f}) 挨了 {damage} 点"
                            f"（第 {local} tick，火团句柄 {lit.handle}，§78/§85）")
                # ★ 火的击退是**常量** `(0, −8)`（§92，语料 1164 发无例外）。
                _emit(machine, machine.sync.event(
                    botsync.OP_SPLASH_DAMAGED,
                    botsync.splash_body(lit.handle,
                                        botsync.character_handle(seat_index),
                                        damage, lit.x, lit.y,
                                        push_x=FIRE_KNOCKBACK[0],
                                        push_y=FIRE_KNOCKBACK[1])))
                _knock_back_seat(room, seat_index, damage, FIRE_KNOCKBACK,
                                 source="地面燃烧")
    # ★ 两条都算烧完了：推到头了，或者**这一刻它本来就该灭了**
    #   （服务端卡了一下、`BOT_FIRE_CATCHUP_TICKS` 那道闸没让它补完）。
    machine.fires = [w for w in machine.fires
                     if w.ticks < w.max_ticks
                     and w.born_tick + w.max_ticks > end]


def _fire_touch(character, px, py, crouched, flames, radius, tick):
    """这个人有没有踩在**这一刻还活着**的某一团火里；踩着了返回那一团。"""
    for cx, cy, r, _region in character.circles(px, py, crouched):
        for flame in flames:
            if not flame.alive(tick):
                continue
            if math.hypot(flame.x - cx, flame.y - cy) <= r + radius:
                return flame
    return None


def _set_ground_on_fire(room, machine, weapon, point):
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
    terrain = mapdata.load(_current_map(room))
    flames = _fire_wall_flames(terrain, flame, point, handle)
    machine.log(f"   火墙: 在 ({point[0]:.1f}, {point[1]:.1f}) 点着 "
                f"{flame.id}({flame.raw.get('section')}) "
                f"{len(flames)}/{step} 团火"
                f"（★ 收方吃掉 {step} 个弹体句柄，§75/§79）")
    _emit(machine, packet)
    machine.fires.append(FireWall(
        handle, flame, flames, time.monotonic(), _fire_wall_ticks(flame)))


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


def _split_shell(room, machine, shell, point, victim_seat):
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
    terrain = mapdata.load(_current_map(room))
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
                Shell(handle + offset, fire_seq, slice_weapon,
                      botsync.FIRE_GROUP_EVERYONE, point[0], point[1],
                      shot, time.monotonic(), max_ticks))
        fire_seq = machine.sync.events
    if not machine.split_logged:
        machine.split_logged = True
        machine.log(f"   分裂: 武器 {shell.weapon.id} 在 "
                    f"({point[0]:.1f}, {point[1]:.1f}) 炸成 "
                    f"{shell.weapon.slice_count} 片 {slice_weapon.id}"
                    f"({slice_weapon.raw.get('section')})，每片吃 "
                    f"{slice_weapon.handle_step} 个句柄（§81）")


def _advance_shells(room, machine, now):
    """把所有在飞的子弹推进到**此刻**，撞上什么就当场结算。

    ## 为什么每帧推一次就够（铁律 10）

    弹道本身是**闭式解**（`ballistics.position_at`），什么时候算都一样；
    唯一会变的是**别人站在哪**，而那个只有真人的心跳到达时才会变 ——
    这个函数正是挂在那一发心跳上的（`_tick_bot` 的第一件事）。
    也就是说：两帧之间根本没有新事实，推早了也算不出别的结果。

    ## 一发都不能漏

    句柄记账在开火那一刻就推进了，少发一发 `rpExplode`，收方那一格计数器
    就和服务端错开，从此每一发都对不上号 —— 打不掉血且一局之内不自愈
    （§42）。所以这个函数排在 `_tick_bot` 的最前面，
    **连「bot 这会儿正躺着」都不挡它**：真人死了，他打出去的子弹照样在飞。
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
    terrain = mapdata.load(_current_map(room))
    bodies_cache = {}
    still = []
    for shell in alive:
        bodies = bodies_cache.get(shell.group)
        if bodies is None:
            # ★ 组 255 = **撞所有人**（分裂弹的碎片就是这个组，§81）——
            #   「所有人」里含射手自己，和溅射、火墙同一个口径（§69 / D50）。
            bodies = _battle_bodies(
                room, machine.my_seat, shell.group,
                include_self=(shell.group == botsync.FIRE_GROUP_EVERYONE))
            bodies_cache[shell.group] = bodies
        # ★ 收方每 32 ms 推一步（`ballistics.TICK_MS`，§47）。这里按**真实
        #   流逝的时间**算它该走到第几步 —— 服务端的帧率（跟着真人心跳走，
        #   ~8 Hz）和它无关，所以帧掉几拍也不会让子弹变慢。
        #   ★ 至少推一步：`_try_fire` 开完枪当场调一次，贴脸那一发
        #     （枪口到人只有几十个单位）就在收方的第一步里结算掉，
        #     不用等下一帧的 125 ms。
        want = max(1, int((now - shell.born) * ballistics.TICKS_PER_SECOND))
        want = min(want, shell.max_ticks)
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
        _resolve_shell(room, machine, shell, landed[0], landed[1], landed[2])
    # ★ `still` 在前、这一轮新生的碎片在后 —— 顺序只影响下一帧的推进次序，
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
        group = _seat_group(room, machine.my_seat)
        bodies = _battle_bodies(room, machine.my_seat, group)
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
            _emit(machine, machine.sync.event(
                botsync.OP_SPLASH_DAMAGED,
                botsync.splash_body(
                    swing.handle, botsync.character_handle(seat_index),
                    damage,
                    x + offset[0] * facing,
                    y + offset[1],
                    push_x=push[0], push_y=push[1])))
            _knock_back_seat(room, seat_index, damage, push,
                             source="bot 近身")
            machine.log(f"   近身: 冲刺打中 座位{seat_index} 的{region}"
                        f" 伤害 {damage} 第{step}帧"
                        f" 句柄 {swing.handle}")
            break
    if frame >= swing.move.total_frame:
        machine.dash_swing = None


def _dash_target(room, machine, seat_index, move):
    """够得着的敌人（最近的那个）；没有返回 `None`。

    判据就是这一招**自己**的伤害圈：任何一个伤害帧的圈能盖住对方，
    就算够得着。够不着一步都不冲 —— 原版真人也不会对着空气双击。
    """
    if machine.battle_pos is None:
        return None
    x, y = machine.battle_pos
    group = _seat_group(room, seat_index)
    hostile = set(t[0] for t in _hostile_targets(room, seat_index))
    bodies = [b for b in _battle_bodies(room, seat_index, group)
              if b[0] in hostile]
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
        return False                       # `/s` 是「站住别动」，那就别冲
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
    machine.log(f"   近身: 冲刺 朝{'右' if direction > 0 else '左'} "
                f"目标 座位{target_seat} {move!r} "
                f"体力 {machine.stamina:.0f}/{_stamina_props().sp_max:.0f}"
                f" 句柄 {handle}（★ 收方也吃掉一个弹体句柄，§64）")
    _emit(machine, packet)
    return True


def _try_fire(room, machine, seat_index, weapon, target, now):
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
    terrain = mapdata.load(_current_map(room))
    max_ticks = _shell_max_ticks(terrain, shot, weapon)
    for offset in range(weapon.shots):
        machine.pending_shots.append(
            Shell(handle + offset, fire_seq, weapon, group,
                  muzzle_x, muzzle_y, shot, now, max_ticks))
    machine.next_fire_at = _reload_after_shot(machine, weapon, now)
    # ★ 松手 = 蓄力清零（`0x51685c: and [char+0x594], 0`，§73）。
    #   下一颗手雷得从头按起。
    machine.charge_at = None
    # ★★ 当场推一步：收方在**它的下一个 tick**（32 ms）就把弹体推进一格，
    #   而 bot 的下一帧要等 125 ms。贴脸那一发（枪口到人常常只有几十个
    #   单位）就在那一格里撞上了 —— 等到下一帧再结算的话，爆炸特效会比
    #   弹体晚上一大截。★ 这是**收方的逻辑步长**，不是观测出来的阈值。
    _advance_shells(room, machine, now)
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


def _tick_bot(room, machine, seat_index):
    """一个 bot 走一帧：算落脚点 -> 发心跳（必要时补一发 `rpJump`）。

    ★★ **一帧 = 真人报了一个新位置**（V0.3 §32）。以前这里是「距上一帧不足
    0.125 秒就跳过」，而驱动它的真人心跳恰好也是 ~8 Hz —— 两个同频的东西
    撞在一起就是**拍频**：抖动让一半的帧落在阈值内被丢掉，bot 的实际帧率
    掉到 4~6 Hz 而且忽快忽慢。用户报的「平移的时候一卡一卡、跳在空中尤其
    一顿一顿」就是它。判据换成「`sync_trail_seq` 变了没有」之后，
    bot 和它跟的那个真人**逐发同步**，一发不多一发不少（铁律 10）。

    ★ **在飞的子弹另算**：`_advance_shells()` 排在所有分支的最前面，
    连「这一帧没有新位置点」「bot 正躺着」都不挡它 —— 那是句柄记账的硬要求
    （D34），漏一发就从此打不掉血。
    """
    if machine.sync.broken:
        return
    if not _battle_started(room):
        # ★★ **`0x0400` 到 `0x0402` 之间一动不许动**（用户 2026-08-27 的日志
        #   里抓到的）：`0x0400` 一广播 bot 就被标记成「加载完」（D4），
        #   而**真人还在读图**，他这段时间照发心跳 ⇒ bot 的帧被驱动起来了。
        #   可这时候 `reset_sync_trails()` **还没跑**（它挂在 `IN_GAME` 上，
        #   要等 `0x0402`），于是 bot 拿着**上一局残留的轨迹和句柄计数器**
        #   开枪 —— 实测开局 37 ms 就打出一发，弹体句柄还是上一局的 200062。
        #   收方那边换图时 `ForceReloadTerrain` 已经把计数器清成 200002 了，
        #   两边从此**对不上号**（§42 那个静默丢弃）。
        #   ⇒ 判据是**房间真的进了 stage 7** 这个事件（`0x0402`），
        #     不是定时器（铁律 10）。
        return
    now = time.monotonic()
    try:
        # ★★ **排在所有分支前面**：在飞的子弹一发都不能漏（见那个函数的
        #   注释）。bot 躺着、`/s` 着、这一帧没有新位置点 —— 都不影响
        #   「上一发子弹该炸了」这个事实。
        _advance_shells(room, machine, now)
        # ★ 地上的火同理：它烧不烧得到人，和 bot 躺没躺着无关（§78）。
        _advance_fires(room, machine, now)
        # ★ 正在进行的那一下近身攻击同理：它的伤害判定是**物理**的
        #   （动作走到第几帧、圈里有没有人），和 bot 躺没躺着无关。
        _advance_dash(room, machine, now)
    except botsync.SyncInvariantError as error:
        machine.sync.broken = True
        machine.log(f"   ★★ 同步流不变式被破坏，已停掉这个 bot 的同步: {error}")
        return
    # ★★ 「躺着没有」这件事**在死亡分支前面**记（§74）：那道 2 秒锁的
    #   起算点是「躺着 -> 站起来」这个**翻转**，翻转的前一半发生在
    #   bot 还躺着的时候。放到后面记的话 `was_lying` 永远是 False，
    #   复活那一帧看不到翻转，锁也就永远不会重新挂上。
    _note_action_lock(room, machine, seat_index, now)
    if _lying_dead(room, seat_index):
        # ★ 死亡处理器 `0x4ffbb7`（虚槽，`Character` / `MyCharacter` 同一格）
        #   里有一句 `mov byte [ebx+0x2b5], 0` —— **客户端一死就把蹲的状态
        #   清掉了**。这边不跟着清的话，两边的记账从此错开一轮：真人还蹲着
        #   时 bot 看不到「翻转」，于是重生后**不发**那一发蹲下（§41）。
        machine.crouched = False
        # ★ 手指也松开：躺着的时候鼠标按下去没有任何意义（§73）。
        machine.charge_at = None
        return

    leader = _follow_target(room, machine)
    if leader is None:
        # 房里还没有任何真人报过位置 ⇒ 我们**不知道**地图上哪里能站。
        # 这时候一发都不发：与其把 bot 摆到一个可能在地形里 / 图外的点上，
        # 不如让客户端按自己加载出来的出生点继续画着（D16）。
        return
    mark = (room.seat_index_of(leader), leader.sync_trail_seq)
    if mark == machine.last_trail_mark:
        # 这一发不是位置心跳（开火 / 爆炸 / AI 消息也走同一条转发路），
        # 或者是同一个位置点又被驱动了一次 —— 没有新事实，不动。
        return
    machine.last_trail_mark = mark

    # ★★★ **重生：先把身体搬到服务端选好的那个出生点**（§91）。
    #   看门狗补 `0x0419` 时已经把坐标记进 `pending_spawn` 并发给了客户端，
    #   客户端那边模型已经站在那儿了 —— 这边不跟着搬，下一发心跳就把它
    #   拽回死亡地点（用户 2026-08-28 报的「瞬移 + 抽搐」的另一半）。
    #   ★ 那个点是**已经落过地**的（`pick_respawn_point` settle 过），
    #     所以这里直接当「站在地上」建 `Body`，不用再查一次地形。
    #   ★ 放在 `/s` 分支**前面**：钉住的 bot 也一样会死、一样要站起来。
    spawn = machine.pending_spawn
    if spawn is not None:
        machine.pending_spawn = None
        machine.battle_pos = (spawn[0], spawn[1])
        machine.body = botmove.Body(spawn[0], spawn[1], on_ground=True)
        machine.move_at = now

    if machine.holding and machine.battle_pos is not None:
        # ★ `/s`：站在原地不动（用户 2026-08-26 要的测试手段）。
        #   **照常发心跳** —— 真人站着不动时也一直在发，停发反而是异常状态。
        #   姿势按「站着」来：踩地、速度 0、不按键、不冲刺、不蹲。
        #   ★ 开火那一段照跑：站住正是为了让房主走开、绕到墙后，
        #     看 bot 隔着地形还打不打得到（`line_blocked`，§29）。
        x, y = machine.battle_pos
        jumped, on_ground, vx, vy, fast_run, crouch = 0, True, 0, 0, False, False
        machine.move_at = now       # 站住期间不积欠时间，放开时才不会跨一大步
    else:
        # ★★ **自己走位**（M5 / §71）：对战房里 bot 按地形自己挪，
        #   闯关房和「还没落地」两种情形 `_own_step()` 会返回 None ——
        #   那就退回 D16 那条老路，回放真人的轨迹。
        terrain = mapdata.load(_current_map(room))
        # ★ 先算一次「站在**现在**这个位置打不打得到」：走不走就看它
        #   （`_move_intent` 的第 1 条）。移动之后下面还会再算一次，
        #   那一次才是真正用来开枪的 —— 枪口坐标必须和这一帧的心跳一致（§62）。
        standing_shot = (None if machine.weapon is None
                         else _fire_target(room, machine, seat_index,
                                           machine.weapon))
        point = _own_step(room, machine, seat_index, terrain,
                          standing_shot, now)
        if point is None:
            rank = room.bot_seats().index(seat_index) + 1
            point = trail_point(leader.sync_trail, BOT_FOLLOW_DISTANCE * rank)
        if point is None:
            return
        x, y, jumped, on_ground, vx, vy, fast_run, crouch = point

    previous = machine.battle_pos
    machine.battle_pos = (x, y)
    # ★ 记下这一帧报出去的地面标志：别人那台机器上 bot 的 `[char+0x128]`
    #   就是它，而夺分模式的一条 ×0.75 按受害者这一格判（§89）。
    machine.on_ground = bool(on_ground)
    direction = _walk_direction(previous, x)
    if direction:
        machine.heading = direction
    # ★ 只有**踩在地上**才说「我按着方向键」：腾空那一段的动画是 `Jump`
    #   （不看掩码），而收方会拿按键覆写空中速度，把抄来的抛体速度冲掉（§39）。
    keys = botsync.walk_keys(direction if on_ground else 0)
    # ★★ 冲刺位抄真人这一段的（§40）—— 他按着右键跑，bot 抄来的坐标就是
    #   1.5 倍步长，不报这一位收方只会按普通走速替它挪，然后被心跳一发发
    #   拽回来。★ 和原版同一个前提：**在地上、真的在走**才算数
    #   （`0x515ced` 进冲刺就要求走路方向非 0），否则会出现真客户端里不存在
    #   的组合（站着冲刺 —— 语料 1003 : 3）。
    fast_run = bool(fast_run) and bool(keys)

    # ★ 起跳**按状态翻转去重**：只有「这一帧真的往前挪了」才补 `rpJump`
    #   （铁律 10 说的那种去重口径）。不去重的话，真人跳完站着不动期间轨迹
    #   不推进，bot 会每一帧都发一发 `rpJump` —— 那是**事件包**，每发都要
    #   吃掉一个可靠序号，动画上还会一直抽。
    moved = previous is not None and (x, y) != previous
    try:
        if jumped and moved:
            # ★ 事件包（内层 < 0x4000）：序号必须严格连续，所以它和心跳里的
            #   N 是同一本账，全在 `BotSyncStream` 里记（D5）。
            _emit(machine, machine.sync.event(
                botsync.OP_JUMP, botsync.jump_body(seat_index, jumped)))
        # ★★ 蹲：心跳里没有这一位，只有 `rpCrouch` 这一发事件包说得着（§41）。
        #   所以**按状态翻转发**（铁律 10 的口径）：和上一帧不一样才发一发，
        #   一样就什么都不做。漏发一次那个姿势就一直错到下次翻转。
        if bool(crouch) != bool(machine.crouched):
            _emit(machine, machine.sync.event(
                botsync.OP_CROUCH, botsync.crouch_body(seat_index, crouch)))
            machine.crouched = bool(crouch)
        # ★★ 开火（M3b）：先挑目标 —— 挑到了的话准星就摆在它身上，
        #   心跳里的朝向位 / 角度 / 正走还是倒走全由 `aim_state()` 跟着变
        #   （§37 / §39）。这正是 `aim_point()` 那段注释预告的换法：
        #   这个游戏的朝向跟**准星**走，「一边后退一边朝身后开枪」是合法姿势。
        weapon = machine.weapon
        target = (None if weapon is None
                  else _fire_target(room, machine, seat_index, weapon))
        cursor = None if target is None else target[1]
        # ★★ 手指按不按着，要在**组心跳之前**算好：心跳 `+15` 那一格就是
        #   蓄力计数器（`[char+0x594]`，packet_api §5.5）—— 报了它，别人
        #   屏幕上才看得见 bot 在攒力气（§73）。
        #   ★ 锁着的那 2 秒里连按都不按（§74）：真人那两秒点鼠标没反应。
        acting = _may_act(machine, now)
        _hold_trigger(machine, weapon, target if acting else None, now)
        # ★★ 地面标志和速度**原样抄真人这一段的**（§35），不从位移反推：
        #   踩在地上走的时候真人报的速度就是 0，反推出来的非零速度会让收方
        #   拿它自己往前推算、和坐标打架 —— 那就是「一跳一跳像在抽搐」。
        # ★★★ 按键掩码是**走路动画的开关**（§39）：填 0 的话收方画站姿、
        #   而且不替它走，位置只被心跳一格一格地拉过去。
        # ★ 准星不传 = 摆在自己正前方（`aim_point`），朝向位和角度跟着它
        #   一起算（§36 / §37）。真人的身体朝向就是这么来的。
        state = botsync.character_state(
            x, y, vx=vx, vy=vy, on_ground=on_ground, facing=machine.heading,
            keys=keys, fast_run=fast_run, cursor=cursor,
            state_byte=_charge_value(machine, now))
        _emit(machine, machine.sync.heartbeat(state))
        # ★ 开火排在心跳**后面**：`rpFire` 里带的是自己的枪口坐标，
        #   让收方先按这一帧的心跳把 bot 挪到位，弹道起点才对得上。
        if BOT_DIAG_FIRE_ANYWHERE:
            _diag_why_not_firing(room, machine, seat_index, weapon, target, now)
        # ★★ 体力：先按这一帧的姿势结算（蹲着回得快、冲刺跑要花），
        #   再决定近身那一下打不打得起。三个速率全是 `GameProps.ini` 的。
        _regen_stamina(machine, now, crouched=bool(crouch), fast_run=fast_run)
        # ★★ **近身冲刺攻击优先于开枪**（§64）：原版这一下会占住整个角色
        #   （`TotalFrame` 那么多帧），真人也开不了枪。够得着就冲，够不着才打枪。
        dashing = acting and _try_dash(room, machine, seat_index, now,
                                       on_ground)
        if (acting and not dashing and machine.dash_swing is None
                and target is not None and now >= machine.next_fire_at
                and _may_fire(machine, weapon)
                and _charge_ready(machine, weapon, target[2], now)):
            _try_fire(room, machine, seat_index, weapon, target, now)
    except botsync.SyncInvariantError as error:
        # ★ 不变式炸了：把**这一个 bot** 的流停掉，别的人一点不受影响（D1）。
        #   继续发只会把收方的收包队列越弄越乱，而「bot 不动」是个看得见的故障。
        machine.sync.broken = True
        machine.log(f"   ★★ 同步流不变式被破坏，已停掉这个 bot 的同步: {error}")


def report_bots_loaded(room, why):
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

    ★ **按状态翻转去重**（铁律 10 的口径）：报过就不再报，直到
    `reset_battle_frame()` 把它清掉 —— 那正好发生在下一轮加载开始的时候。
    """
    for index in room.bot_seats():
        seat = room.seats[index]
        machine = None if seat is None else seat.conn
        if not isinstance(machine, BotConn) or machine.sync.broken:
            continue
        if machine.load_progress == botsync.LOAD_PROGRESS_MAX:
            continue
        try:
            machine.load_progress = botsync.LOAD_PROGRESS_MAX
            _emit(machine, machine.sync.volatile(
                botsync.OP_LOAD_PROGRESS,
                botsync.load_progress_body(botsync.LOAD_PROGRESS_MAX)))
        except Exception as error:          # noqa: BLE001 —— 同 `tick_room`
            machine.sync.broken = True
            machine.log(f"   ⚠ bot 进度条出错（{why}），已停掉它的同步: {error!r}")


def _emit(machine, packet):
    """把一份合成好的 `UdpPacket` 交给现成的投递路（**不新增第二条**）。"""
    return machine.sync.deliver(packet, gameserver.PEER_RELAY.deliver)


def tick_room(sender):
    """房里每个 bot 走一帧。**由真人的同步包到达驱动**（D17）。

    `sender` 是刚刚发来同步数据的那条真人连接 —— `gameserver` 在
    `_relay_battle_tick()` 里调本函数，而那个回调挂在 `RelayServer.deliver()`
    上，是原版中继和 `0x040f` 两条路唯一的汇合点（§160）。

    ★ 为什么不起一个定时器线程：房间**只有在真人真的在打**的时候才需要 bot
    动，而「真人在打」这件事本身就是一串 8 Hz 的事件流，服务端手上就有。
    真人全都卡住 / 全都在加载时 bot 跟着停，这正是想要的行为。

    ★ 加载阶段**不走这里**：bot 的进度条是在广播 `0x0400` / `0x0417` 那一刻
    一次性报满的（`report_bots_loaded`，D26），不需要逐帧驱动。

    ★ 抛出去的异常一律吞掉：本函数是在**真人的转发路径**上跑的，
    bot 出问题不能连累真人的同步（D1）。
    """
    room = sender.lobby_room()
    if room is None or not room.is_playing():
        return
    for index in room.bot_seats():
        seat = room.seats[index]
        machine = None if seat is None else seat.conn
        if not isinstance(machine, BotConn):
            continue
        try:
            _tick_bot(room, machine, index)
        except Exception as error:          # noqa: BLE001 —— 见 docstring
            machine.sync.broken = True
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
