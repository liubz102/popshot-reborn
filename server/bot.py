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
- **bot 的每座位状态直接住在 `lobby.Seat` 里**（昵称 / 角色 / 队伍 / 准备
  + 新加的 `is_bot`），不另立一份 bot 座位表（D9）。

## 导入方向（★ 别反过来）

`bot.py` **可以** `import gameserver`（`BotConn` 要拿 `Conn` 当基类）；
`gameserver.py` 反过来只在 `on_chat()` 里**函数内**惰性 `import bot`。
两边都写成模块级 import 就是循环导入 —— `class BotConn(gameserver.Conn)`
会在 `gameserver` 才执行到一半、`Conn` 还没定义的时候炸掉。

## 相关决定

- D1 —— 为什么是「假连接对象」而不是 `Seat.conn = None`；
- D2 —— 房主迁移为什么跳过 bot 座；
- D6 —— `/char N M` 的 M 为什么是面板序号 1..14 而不是原始角色 id；
- D7 —— bot 占座时真人为什么照常被「房间已满」挡住。
"""
from __future__ import annotations

import contextlib
import threading
import time

from account_store import BASE_CHARACTER_IDS, MINIMUM_PLAYER_LEVEL, \
    PREMIUM_CHARACTER_IDS
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
        # ★ **不调 `register_conn()`**：`_conns` 是「在线的真人」表。
        #   进去的话 `latest_conn()`（控制通道不指定账号时的默认目标）
        #   随时可能变成一个 bot，`tools/gs_ctl.py` 就对着空气发命令了。

    def __repr__(self):
        return f"<BotConn {self.nickname} 座位 {self.my_seat}>"

    # -- 发送：全部空操作 ---------------------------------------------------
    def send(self, plain):
        """空操作。bot 没有客户端，字节发出去也没人解。

        ★ 返回而不是抛异常：`battle_broadcast()` / `broadcast()` 会对房里
        每一个成员调它，抛异常等于让 bot 的存在弄坏真人的广播。
        """
        return

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
