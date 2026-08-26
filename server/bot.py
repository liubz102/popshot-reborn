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
- D6 —— `/char N M` 的 M 为什么是面板序号 1..14 而不是原始角色 id；
- D7 —— bot 占座时真人为什么照常被「房间已满」挡住；
- D16 —— bot 的落脚点为什么是「回放真人的轨迹」而不是自己算；
- D17 —— bot 的帧为什么由真人的同步包驱动，而不是定时器线程。
"""
from __future__ import annotations

import collections
import contextlib
import math
import threading
import time

from account_store import BASE_CHARACTER_IDS, MINIMUM_PLAYER_LEVEL, \
    PREMIUM_CHARACTER_IDS
import botsync
import gameserver
import lobby as lobby_module
import relayserver
import udpsync
from lobby import ROOM_SEAT_COUNT, Seat, TEAM_A, TEAM_B, TEAM_LAYOUT_TEAMS

#: bot 的昵称：`bot <座位号>`。
#:
#: ★ 座位号用的是**服务端座位号 0..5**，和日志、`0x0301` 的座位字段、
#: `/del N` 里的 N 完全一致 —— 玩家在聊天窗口看到「bot 3」，敲的就是
#: `/del 3`，不用在脑子里做任何换算。
BOT_NICKNAME_PREFIX = "bot "

#: bot 座位里发下去的等级（`SessionSlot+0x10`）。
#:
#: 房间里按「开始」时客户端只读**房主座位**的等级（V0.1 §77），bot 永远不是
#: 房主，所以这个数不影响任何准入判定 —— 它纯粹是玩家列表里显示的那一格。
#: 取 `MINIMUM_PLAYER_LEVEL`（真人下发等级的下限）是为了看起来不突兀。
BOT_LEVEL = MINIMUM_PLAYER_LEVEL

#: 「人物选择」面板上的角色顺序 —— `/char N M` 里的 M 就是这张表的 1-based 下标。
#:
#: 原始角色 id 是 `0/1/2` + `100..110`，中间断了一大截（id 3 아이린 和 98
#: 쉐도우 타이 被客户端的按钮循环 `0x4f58e8` 显式跳过，99 랜덤 要另一个开关，
#: 三个都放不出来，见 `account_store.PREMIUM_CHARACTER_IDS` 的注释）。
#: 直接让玩家写原始 id 的话 `/char 3 5` 这种自然写法就是非法值 —— 所以命令里
#: 用连续的面板序号，只在服务端换算一次（D6）。
CHARACTER_PANEL_IDS = tuple(BASE_CHARACTER_IDS) + tuple(PREMIUM_CHARACTER_IDS)

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
#: 这就是「`/team` 没反应」的全部原因，所以换队命令改叫 `/tm`。
CLIENT_RESERVED_PREFIXES = ("/team ", "/팀 ", "/say ", "/tell ", "/to ", "/귓 ")

#: 造过几个 bot（只用来给日志编号，和座位号无关）。
_bot_seq = 0
_bot_seq_lock = threading.Lock()


def _next_bot_seq():
    global _bot_seq
    with _bot_seq_lock:
        _bot_seq += 1
        return _bot_seq


def character_for_panel(panel_index):
    """面板序号 1..14 -> 原始角色 id。越界返回 ``None``（调用方要报错）。"""
    try:
        index = int(panel_index)
    except (TypeError, ValueError):
        return None
    if not 1 <= index <= len(CHARACTER_PANEL_IDS):
        return None
    return CHARACTER_PANEL_IDS[index - 1]


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
        #: ★ 服务端一点地图几何都没有（M4 才有），所以这个锚点是**跟着真人
        #:   走**的 —— 真人此刻站着的地方一定是合法地面（D16）。
        self.battle_pos = None
        #: 现在朝哪走：`+1` 右 / `-1` 左。
        self.heading = botsync.FACING_RIGHT
        #: ★ 上一帧消费到的是**谁的第几个位置点**：`(座位号, sync_trail_seq)`。
        #:   bot 的帧就是靠它对齐的 —— 号变了 = 真人报了新位置 = 走一帧
        #:   （V0.3 §32，代替了原来那个 0.125 秒的采样率限流）。
        self.last_trail_mark = None
        #: 这一轮加载报过那一发 `0x4005` 了吗（D26）。
        #: `None` = 还没报。报过之后恒为 100，拿它**按状态翻转去重**。
        self.load_progress = None
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
        """
        self.battle_pos = None
        self.last_trail_mark = None
        self.load_progress = None

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
#: 占那 4 行的额度。50 是实测过的安全值 —— 旧版最长那行（`/char` 那条）
#: 54 宽在框里没折，往回收一点留余量。改这张表时请用同样的口径数宽度
#: （中文和全角标点算 2，ASCII 算 1）。
HELP_LINES = (
    "bot 命令（房主专用，N = 座位号，/h 重看）：",
    "/bot 加一个;  /del N 删掉;  /ready 全部准备",
    "/char N M 换角色（M=1~14）;  /tm N 换队（组队战）",
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
#: ★ `team` 还在表里，但它**不是**换队命令，只是一行「请改用 /tm」的提示
#: —— 客户端自己把 `/team ` 当队伍聊天吃掉了，服务端根本收不到（§19）。
COMMAND_NAMES = ("bot", "del", "char", "tm", "team", "ready", "help", "h", "?")


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


def _cmd_bot(conn, room, args):
    """`/bot` —— 最小空座加一个 bot。"""
    index = room.free_seat()
    if index is None:
        return "房间已经满了（6 个座位都有人），先 /del 掉一个再加。"
    machine = BotConn(index)
    seat = machine.seat_snapshot()
    index = gameserver.LOBBY.add_bot(room, seat)
    if index is None:                      # 拿锁那一刻被别人坐满了
        return "房间刚好被坐满了，没加上。"
    _align_epoch(machine, room)
    conn.log(f"   /bot: 座位 {index} 加入 {seat.nickname}"
             f"（角色 {seat.character_id} 队伍 {seat.team}）")
    conn.online(f"房间 + bot 房间 #{room.room_id} 座位={index} "
                f"房主={conn.account_name!r}")
    # ★ action 0 是唯一会**建**座位 3D 模型的分支（`0x405e1c`），换角色那个
    #   action 4 建不出来。`broadcast_seat_slot` 发给房里每一个人**含房主
    #   自己** —— 房主的客户端和别人一样，只认这一发。
    conn.broadcast_seat_slot(room, index, gameserver.SEAT_ACTION_JOIN,
                             reason=f"：座位 {index} 加入 bot")
    conn.room_system_chat(f"{seat.nickname} 加入了房间。")
    return _team_balance_warning(room)


def _cmd_del(conn, room, args):
    """`/del N` —— 删掉一个 bot。"""
    index, error = _seat_arg(args)
    if error:
        return f"/del 用法：/del <座位号>。{error}"
    seat, error = _bot_seat(room, index)
    if error:
        return f"删不掉：{error}"
    nickname = seat.nickname
    if gameserver.LOBBY.remove_bot(room, index) is None:
        return f"删不掉：{index} 号位刚刚已经空了。"
    conn.log(f"   /del: 座位 {index} 的 {nickname} 已删除")
    conn.online(f"房间 - bot 房间 #{room.room_id} 座位={index} "
                f"房主={conn.account_name!r}")
    # ★ 必须发 action 1，不能发 3 —— 只有 1/2 会走 `0x405f8f` 把座位的 3D
    #   模型销毁掉。发 3 的话名字没了、模型还杵在天上（§147）。
    conn.broadcast_seat_leave(index, reason=f"：座位 {index} 的 bot 被删除")
    conn.room_system_chat(f"{nickname} 离开了房间。")
    return _team_balance_warning(room)


def _cmd_char(conn, room, args):
    """`/char N M` —— 换 bot 的角色。M 是面板序号 1..14（D6）。"""
    index, error = _seat_arg(args)
    if error:
        return f"/char 用法：/char <座位号> <角色序号 1~14>。{error}"
    if len(args) < 2:
        return "/char 用法：/char <座位号> <角色序号 1~14>。少了角色序号。"
    character = character_for_panel(args[1])
    if character is None:
        return (f"角色序号 {args[1]!r} 不对，只能是 1~{len(CHARACTER_PANEL_IDS)}"
                f"（1~3 基础角色，4~14 商城角色）。")
    seat, error = _bot_seat(room, index)
    if error:
        return f"换不了角色：{error}"
    seat.update(character_id=character)
    if isinstance(seat.conn, BotConn):
        seat.conn.character_id = character
    conn.log(f"   /char: 座位 {index} 的 {seat.nickname} -> "
             f"面板 {args[1]} = 角色 id {character}")
    # ★ 用 action 3（按座位数据重建模型）而不是 action 4：后者会让客户端播
    #   一句韩文「%s님이 %s 캐릭터로 선택되었습니다.」（`0x406520`）。
    #   我们自己用中文说一遍就够了。
    conn.broadcast_seat_slot(room, index, gameserver.SEAT_ACTION_RESYNC,
                             reason=f"：座位 {index} 的 bot 换角色")
    conn.room_system_chat(f"{seat.nickname} 换成了 {args[1]} 号角色。")
    return None


def _cmd_team(conn, room, args):
    """`/tm N` —— bot 换队（1↔2）。只有组队战有效。

    ★ **命令名不能叫 `/team`**：客户端把 `/team ` 当成队伍聊天的前缀自己吃掉了
    （见 `CLIENT_RESERVED_PREFIXES` / §19），服务端一个字都收不到。
    """
    index, error = _seat_arg(args)
    if error:
        return f"/tm 用法：/tm <座位号>。{error}"
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
    conn.log(f"   /tm: 座位 {index} 的 {seat.nickname} -> {want} 队")
    conn.broadcast_seat_slot(room, index, gameserver.SEAT_ACTION_RESYNC,
                             reason=f"：座位 {index} 的 bot 换队")
    conn.room_system_chat(f"{seat.nickname} 换到了 {want} 队。")
    return _team_balance_warning(room)


def _cmd_ready(conn, room, args):
    """`/ready` —— 所有 bot 一键准备。"""
    seats = room.bot_seats()
    if not seats:
        return "房间里一个 bot 都没有，先 /bot 加一个。"
    changed = []
    for index in seats:
        seat = room.seats[index]
        if seat is None or seat.ready:
            continue
        seat.update(ready=True)
        changed.append(index)
        # ★ 和真人按「游戏准备」走同一条路（`on_toggle_ready`）：action 3。
        #   房里其他人的「准备中」标记、房主能不能按开始，全靠这一发。
        conn.broadcast_seat_slot(room, index, gameserver.SEAT_ACTION_RESYNC,
                                 reason=f"：座位 {index} 的 bot 准备")
    if not changed:
        return f"{len(seats)} 个 bot 本来就都准备好了。"
    conn.log(f"   /ready: 座位 {changed} 的 bot 已准备")
    conn.room_system_chat(f"{len(changed)} 个 bot 准备好了。")
    return _team_balance_warning(room)


def _cmd_team_alias(conn, room, args):
    """`/team` —— 只回一行「请改用 /tm」。

    ★ 带参数的 `/team 1` **永远走不到这里**：客户端匹配的是 `"/team "`
    （连空格），中了就切掉前缀当队伍聊天发出去（§19）。不带参数的光杆
    `/team` 差那个空格，反而能原样送到服务端 —— 这一条就是为它准备的，
    玩家敲错时至少能看见该敲什么。
    """
    return "换队请敲 /tm N（例：/tm 1）—— /team 被客户端当成队伍聊天吃掉了。"


def _cmd_help(conn, room, args):
    for line in HELP_LINES:
        conn.send_system_chat(line)
    return None


COMMANDS = {
    "bot": _cmd_bot,
    "del": _cmd_del,
    "char": _cmd_char,
    "tm": _cmd_team,
    "team": _cmd_team_alias,
    "ready": _cmd_ready,
    "help": _cmd_help,
    "h": _cmd_help,
    "?": _cmd_help,
}

#: 改房间状态的命令 —— 游戏中一律拒绝。`/help` 不在里面，随时能看。
#: `team` 也不在里面：它只是一行「请改用 /tm」的提示，什么都不改。
MUTATING_COMMANDS = ("bot", "del", "char", "tm", "ready")


# ----------------------------------------------------------------------------
# 战斗中：让 bot 在别人屏幕上动起来（M3）
# ----------------------------------------------------------------------------
#: bot 跟在真人**多远**的后面（游戏内坐标单位）。
#:
#: 房里有多个 bot 时按座位次序排队（第 N 个 bot 跟 N 倍远），免得几个 bot
#: 叠在同一个点上变成一个人。
BOT_FOLLOW_DISTANCE = 120.0


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

    返回 `(x, y, jumped, on_ground, vx, vy)`：坐标是插出来的，后四个是真人
    **在这一段**的原样事实（起没起跳、踩地还是腾空、腾空时的速度）。
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
    """一个轨迹点的 `(jumped, on_ground, vx, vy)`。

    ★ 老式三元组（单测里手搓的假轨迹、以及本版之前落盘的那种）没有后三个
    字段：那时候补上「踩在地上、速度 0」—— 那是**地面行走**，也正是
    没有更多信息时唯一安全的假设（腾空却说踩地，最多少一段抛物线姿势；
    反过来说腾空则会让收方拿一个假速度推算，直接抽搐）。
    """
    jumped = point[2] if len(point) > 2 else 0
    if len(point) >= 6:
        return jumped, bool(point[3]), point[4], point[5]
    return jumped, True, 0, 0


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
    """
    if machine.sync.broken:
        return
    if _lying_dead(room, seat_index):
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

    rank = room.bot_seats().index(seat_index) + 1
    point = trail_point(leader.sync_trail, BOT_FOLLOW_DISTANCE * rank)
    if point is None:
        return
    x, y, jumped, on_ground, vx, vy = point

    previous = machine.battle_pos
    machine.battle_pos = (x, y)
    direction = _walk_direction(previous, x)
    if direction:
        machine.heading = direction
    # ★ 只有**踩在地上**才说「我按着方向键」：腾空那一段的动画是 `Jump`
    #   （不看掩码），而收方会拿按键覆写空中速度，把抄来的抛体速度冲掉（§39）。
    keys = botsync.walk_keys(direction if on_ground else 0)

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
        # ★★ 地面标志和速度**原样抄真人这一段的**（§35），不从位移反推：
        #   踩在地上走的时候真人报的速度就是 0，反推出来的非零速度会让收方
        #   拿它自己往前推算、和坐标打架 —— 那就是「一跳一跳像在抽搐」。
        # ★★★ 按键掩码是**走路动画的开关**（§39）：填 0 的话收方画站姿、
        #   而且不替它走，位置只被心跳一格一格地拉过去。
        # ★ 准星不传 = 摆在自己正前方（`aim_point`），朝向位和角度跟着它
        #   一起算（§36 / §37）。真人的身体朝向就是这么来的。
        state = botsync.character_state(
            x, y, vx=vx, vy=vy, on_ground=on_ground, facing=machine.heading,
            keys=keys)
        _emit(machine, machine.sync.heartbeat(state))
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
