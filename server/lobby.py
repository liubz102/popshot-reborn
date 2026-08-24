#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大厅房间表 —— V0.2 里程碑 I 的共享状态。

「谁在哪个房间、房间里有哪些座位」全部记在这里。认证服和游戏服合并成了
**一个进程**（D064），所以不需要任何跨进程同步 —— 一把锁就够。

这个模块**只管模型，不碰协议**：不组包、不发包、不 import `gameserver`。
理由是它要能被单独测（不需要 socket、不需要账号存储），而且组包那一侧
（`gameserver.py`）已经有 3000 行了，再往里塞状态机会彻底失控。

线格式和 opcode 的事实见 `.claude/FINDINGS.md`：
§137（`Session` 布局）、§138（`0x0200` 房间列表）、§140（`0x0202` 加入房间）。
"""
from __future__ import annotations

import threading

#: 一个房间六个座位（`0x0300` 快照固定发 6 项，V0.1 §79）。
ROOM_SEAT_COUNT = 6

#: `Session+0x04` 房间状态。2 = 待机中，其余一律被客户端当「游戏中」（V0.1 §102）。
#: ★ 待机房间必须是 2，否则房间场景一片黑。
SESSION_STATUS_WAITING = 2
SESSION_STATUS_PLAYING = 3

#: `0x0202 gspRepMoveInto` 的结果码（FINDINGS §140）。文案全是客户端自带的，
#: 和 D069 / D071 一样：回对码就行，一个字都不用我们写。
MOVE_INTO_OK = 0                #: 进房成功
MOVE_INTO_ALREADY_PLAYING = 1   #: 「此房间已开始游戏。」
MOVE_INTO_FULL = 2              #: 「已超出人数限制的房间。」
MOVE_INTO_NO_SUCH_ROOM = 3      #: 「没有符合条件的房间。」
MOVE_INTO_BAD_PASSWORD = 4      #: 「密码错误。」

#: 房间类型 -> 大厅标签页的游戏类型。`0x0200` 请求里带的就是游戏类型
#: （FINDINGS §139），用它过滤「对战标签只看普通房」。
#: 类型 3 / 4 服务端从不下发（客户端自己的反序列化 bug，见 gameserver 里
#: `DESCRIPTOR_READ_ARGUMENT_COUNTS` 的注释），所以这里也不列。
SESSION_TYPE_GAME_TYPES = {0: 1, 1: 1, 2: 2, 5: 5, 6: 6}

#: 队伍编号。客户端的「变更队伍」按钮在 1 和 2 之间来回切
#: （`0x469f95` / `0x46deaa`：`cmp byte[slot+8],1 / sete al / inc al`），
#: 0 = 还没分队。详见 FINDINGS §165。
TEAM_NONE = 0
TEAM_A = 1
TEAM_B = 2

#: 一个房间怎么分队。
#:
#: ★★★ **队伍号只能取 0 / 1 / 2**（bug调查/8_2 §212）。客户端的
#: `QuestVictoryCondition` 里「队伍记录」数组**只有两格**
#: （`this + 40*队伍序号`，队伍序号 = 队伍号 - 1），而每次死亡广播都会走
#: `0x48c942 -> 0x55c5f2 -> vf34(0x55c696)` 把每个座位的战绩累加进队伍记录，
#: **那里一个上界检查都没有**。队伍号 >= 3 时写出去的地址正好压在
#: **别人的每座位战绩记录**上（座位记录 `this + 座位*0x2c`，字段
#: +0x5c 分数 / +0x60 死亡 / +0x64 最大生命）：
#:
#:     队伍号 3（座位 2）-> this+0x50 .. 0x70  踩座位 0 的战绩
#:     队伍号 4（座位 3）-> this+0x78 .. 0x98  踩座位 1 的战绩
#:
#: 后果是别人的**死亡次数**被越写越大，而生存模式的剩余生命
#: `= max(0, 3 - 死亡次数)`（`0x55e0a3`，最大生命恒为 3 见 `0x55db69`），
#: 一旦被踩到 >= 3 就变成 0 条命 —— 受害者**活着也会被切进观战画面**，
#: **死了 `Die()` 把 `[char+0x2d8]` 写成 -1 从此永不重生**。
#: 两人局（队伍号 1 / 2）不越界，所以这个 bug 只在**3 人以上的个人战**出现，
#: 和线上「3个人就BUG」的实测完全一致。
#:
#: ⚠ 旧注释说「队伍号相同就不结算伤害，所以个人战必须人人不同号」——
#: **那是误读**（D092）。`0x4fedfc` 两条分支在 `0x4fee6b` 汇合，扣血
#: （`[ebp-0x18] - [ebp+0xc]`）在汇合点之后**无条件执行**；队伍号相同只是
#: 跳过 `0x4087c2` 那发**伤害统计**记账。所以个人战填 0 打得动人。
#:
#: `TEAMS` 组队战：座位号奇偶交替 1 / 2。客户端自己填 Dummy 座位就是这条
#:        （`0x468952`：`idiv 2 ; inc dl ; mov [slot+8], dl`）。
#:        ⚠ 这个模式下客户端**要求两队人数相等**才让开局
#:        （`0x468495` 数 1 队和 2 队，不等就回错误码 3 ->「两组人数不相同。
#:        请调整人数。」），所以 3 人 / 5 人的组队战本来就开不起来。
#: `FREE` 个人战：**全部 0 = 没分队**（`0x48c942` 见 0 就整段跳过队伍记账）。
#: `COOP` 闯关等：全在 1 队 —— 队友之间不该有伤害。
TEAM_LAYOUT_TEAMS = "teams"
TEAM_LAYOUT_FREE = "free"
TEAM_LAYOUT_COOP = "coop"

#: 房间描述符 type == 1（普通对战）时，`arguments[0] == 1` = 组队战。
#: 客户端的 `0x409df1` 对 type 1 返回的就是 `arguments[0]`，全部「要不要
#: 分队」的判断（房间里的站位、名牌颜色、队伍人数检查）都读它。
SESSION_TYPE_NORMAL = 1
NORMAL_ARGUMENT_TEAM_MODE = 1

#: 描述符 type == 1 时 `arguments[2]` = **道具模式**（1 = 아이템전 / 道具模式，
#: 0 = 노템전 / 普通模式）。客户端的 `0x409dd9` 对 type 1 返回的就是它，
#: 对别的 type 恒返回 0 / -1 —— 也就是说**只有普通对战房才有道具模式**（§190）。
NORMAL_ARGUMENT_ITEM_MODE_INDEX = 2

#: 游戏模式（`arguments[1]`，客户端的 `0x409e0a`）== 2 时，客户端在房间面板里
#: **强制**把道具标志清 0（`0x465be2`）。服务端跟着它判，免得出现
#: 「房间面板显示노템전、服务端却在刷道具」。
NORMAL_ARGUMENT_MODE_INDEX = 1
MODE_WITHOUT_ITEMS = 2


def team_layout_of(session_type, arguments):
    """这个房间该按哪种口径分队。见 `TEAM_LAYOUT_*`。"""
    if int(session_type) != SESSION_TYPE_NORMAL:
        return TEAM_LAYOUT_COOP
    first = arguments[0] if arguments else 0
    return (TEAM_LAYOUT_TEAMS if int(first) == NORMAL_ARGUMENT_TEAM_MODE
            else TEAM_LAYOUT_FREE)


def item_mode_of(session_type, arguments):
    """这个房间是不是**道具模式**（아이템전）。见 §190。

    判据和客户端一模一样：
    ① 只有 `type == 1`（普通对战）才有道具模式 —— `0x409dd9` 对别的 type
       返回 0（天梯）或 -1（闯关等），客户端自己也不会显示那个开关；
    ② `arguments[1] == 2` 那个游戏模式下客户端强制无道具（`0x465be2`）；
    ③ 其余情况看 `arguments[2]`。
    """
    if int(session_type) != SESSION_TYPE_NORMAL:
        return False
    arguments = tuple(arguments)
    if len(arguments) > NORMAL_ARGUMENT_MODE_INDEX and \
            int(arguments[NORMAL_ARGUMENT_MODE_INDEX]) == MODE_WITHOUT_ITEMS:
        return False
    if len(arguments) <= NORMAL_ARGUMENT_ITEM_MODE_INDEX:
        return False
    return int(arguments[NORMAL_ARGUMENT_ITEM_MODE_INDEX]) == 1


def default_team(seat_index, layout=TEAM_LAYOUT_TEAMS):
    """一个座位在某种口径下默认属于哪一队。

    ★ 返回值只允许 `TEAM_NONE` / `TEAM_A` / `TEAM_B` —— 客户端的队伍记录
    数组只有两格，>= 3 会越界写进别人的战绩（见 `TEAM_LAYOUT_*` 的说明）。
    """
    index = int(seat_index)
    if layout == TEAM_LAYOUT_TEAMS:
        return (index % 2) + 1
    if layout == TEAM_LAYOUT_FREE:
        return TEAM_NONE
    return TEAM_A


class Seat:
    """房间里的一个座位。

    昵称 / 等级 / 角色 id / 队伍 / 准备状态存的是**快照**：`0x0300` 和 `0x0301`
    要把它们发给房里每一个人，而别人的连接查不到你的存档。换角色
    （`0x0301` action 4）、变更队伍、按「游戏准备」和重新登录时由 `gameserver`
    调 `update()` 刷新。
    """

    __slots__ = ("conn", "username", "nickname", "level", "character_id",
                 "team", "ready", "is_bot")

    def __init__(self, conn, username="", nickname="", level=1, character_id=0,
                 team=TEAM_NONE, ready=False, is_bot=False):
        self.conn = conn
        self.username = username
        self.nickname = nickname
        self.level = int(level)
        self.character_id = int(character_id)
        #: 队伍（`SessionSlot+0x08`）。只有组队模式（描述符 `arguments[0] == 1`）
        #: 的客户端会读它 —— 房间里的站位、战斗里的友军伤害判定都靠它。
        self.team = int(team)
        #: 准备好了没有（`SessionSlot+0x2e`）。房主那一格客户端**不看**
        #: （`0x4696f8` 直接把房主算成已准备），非房主必须靠服务端广播。
        self.ready = bool(ready)
        #: ★ 这格坐的是 bot（V0.3）。判据放在**座位**上而不是「conn 是不是
        #: None」：bot 的座位有一个假连接对象（`bot.BotConn`，V0.3 D1），
        #: 而调试通道造的假房间座位 conn 才是 None —— 两者必须分得开（D2）。
        self.is_bot = bool(is_bot)

    def update(self, nickname=None, level=None, character_id=None,
               team=None, ready=None):
        if nickname is not None:
            self.nickname = nickname
        if level is not None:
            self.level = int(level)
        if character_id is not None:
            self.character_id = int(character_id)
        if team is not None:
            self.team = int(team)
        if ready is not None:
            self.ready = bool(ready)

    def snapshot(self):
        """给 `build_session_slot(**...)` 直接展开用的 kwargs。"""
        return {
            "occupied": True,
            "nickname": self.nickname,
            "level": self.level,
            "character_id": self.character_id,
            "team": self.team,
            "ready": self.ready,
        }


class Room:
    """一个房间。

    `room_id` 同时是三处的那个号：`0x0200` 列表里每项的 u16、`0x0202` 请求里
    的 int32、以及 `0x0201` / `0x0202` 应答里回给 `LobbyStage+0x1c8` 的
    session id。客户端在房间列表上显示的是 **`room_id + 1`**（`'%d번'`，§138），
    所以号从 0 开始分配，玩家看到的就是 1 号房、2 号房。
    """

    def __init__(self, room_id, host_conn, *, title="", map_name="",
                 session_type=1, arguments=(), password="",
                 status=SESSION_STATUS_WAITING):
        self.room_id = int(room_id)
        self.title = title
        self.map_name = map_name
        #: ★ 「随机地图」开关（`Session` 的第 5 个字段 -> `LobbyStage+0x14`）。
        #: 房主在「选择地图」面板上点那颗「랜덤 / 随机」按钮时由 `0x0302` 报上来。
        #: 打开之后，客户端在收到 `0x0400 gspPrepareGame` 时会用包里的 **seed**
        #: 自己挑一张图（全房间同一个 seed = 同一张图），房主再回一发 `0x0302`
        #: 把结果告诉我们。服务端**不需要**知道地图几何，只要把这个开关原样传回去
        #: —— 不传的话客户端下一发 `0x0303` 就把它清成 0，按钮当场弹回（§228）。
        self.random_map = False
        self.session_type = int(session_type)
        self.arguments = tuple(arguments)
        self.password = password or ""
        self.status = int(status)
        self.seats = [None] * ROOM_SEAT_COUNT
        #: 房主坐哪个座位。建房的人固定坐 0 号（客户端 `0x54f807` 自己写死的，
        #: V0.1 §75），房主走人之后转给还在的最小座位号。
        self.host_seat = 0
        self.host_conn = host_conn
        #: 本房间的开局握手状态（`gameserver.RoomStartGame`）。
        #: ★ `lobby.py` **只管模型不碰协议**，所以这里只留一个空槽，
        #: 建和用都在 `gameserver.py` 里 —— 这样 `lobby.py` 仍然能单独测。
        self.battle = None
        #: 本房间**这一局关卡**的战斗状态（`gameserver.RoomQuest`）。
        #: 掉落物句柄、拾取仲裁、每座位的死亡次数和分数、换图等人 —— 全在里面。
        #: 和 `battle` 分开是因为生命周期不同：`battle` 管「怎么开起来」，
        #: `quest` 管「开起来之后」，回房间时后者整个丢掉重建。
        self.quest = None
        #: ★★ 本房间当前的**代号**和**代的种类**（§218 / D137）。
        #:
        #: 客户端的局号 `[GameSession+0x3c]` 是「每座位收包队列的纪元号」，
        #: 它每变一次，六条队列就被清空一次。房里的人是不是处在**同一代**
        #: （同一次清空之后），决定了他们的同步数据能不能互相投递 ——
        #: 这就是这两个字段存在的唯一理由。
        #:
        #: 和 `battle` / `quest` 一样：`lobby.py` **只管模型不碰协议**，
        #: 代号由 `gameserver` 注入的发号器给（`advance_generation`），
        #: 谁在什么时候推进它也全写在那边。
        self.epoch_gen = None
        self.epoch_kind = None
        #: ★ 本房间**当前这一代的局号**。房里每个人都应该是这个数：
        #: 新房间从 0 起（进房那一发 `0x0303` 就把客户端设成它），
        #: 之后每换一代 +1（`0x0400` 一次、`0x0403` 一次）。
        #: 中途进房的人靠 `0x0303` 当场对齐，不需要任何补发。
        self.epoch_value = 0
        #: ★ `0x0403` 是**每个人看完结算各自触发**的，也就是**各自的线程**在
        #: 调 `advance_generation` —— 不上锁的话两条线程会各分配一个「房间代」，
        #: 房里的人被劈成两代，整个房间阶段互相收不到包（下一发 `0x0400` 才
        #: 自愈）。`0x0400` 那一路是单线程广播，不需要，但共用同一把锁最省事。
        self._epoch_lock = threading.Lock()

    # -- 换代 ---------------------------------------------------------------
    def advance_generation(self, kind, allocator):
        """把房间推进到 `kind` 这一代，返回代号。**同 kind 重复调是空操作。**

        `kind` 只有两个值：

        * ``"battle"`` —— 广播 `0x0400 gspPrepareGame`（全员切 stage 6 加载关卡）；
        * ``"room"``   —— 有人看完结算、收到 `0x0403` 切回 stage 5 房间。

        ★ 幂等是**必须**的，不是优化：`0x0400` 要发给房里每一个人，
        而 `0x0403` 是每人看完结算各自触发的（前后可能差十几秒）——
        这两组包必须落在**同一个代号**里，否则同一局的人会被判成互相跨代，
        同步数据全被丢掉。

        房间刚建出来时 `epoch_gen` 是 None，第一次调用无论 kind 是什么都会
        分配一个号（新房间里没人打过，所有人都在「房间代」）。
        """
        with self._epoch_lock:
            if self.epoch_gen is None:
                self.epoch_kind = kind
                self.epoch_gen = allocator()
            elif self.epoch_kind != kind:
                self.epoch_kind = kind
                self.epoch_gen = allocator()
                #: 换一代局号就 +1 —— 客户端那边是 `inc [GameSession+0x3c]`，
                #: 这里跟着走同一格，进房的人才能拿到正确的数。
                #: （房间刚建出来那一次不算换代，所以只在 elif 里加。）
                self.epoch_value += 1
            return self.epoch_gen

    # -- 查询 ---------------------------------------------------------------
    @property
    def game_type(self):
        return SESSION_TYPE_GAME_TYPES.get(self.session_type, 1)

    def player_count(self):
        """坐了人的座位数，**bot 也算一个人**。

        房间列表上的「3/6人」和客户端自己数出来的空位数（`0x556f40`）都按
        座位算，所以这里不能把 bot 摘出去 —— 摘了两边就对不上。
        """
        return sum(1 for s in self.seats if s is not None)

    def is_empty(self):
        return self.player_count() == 0

    # -- bot（V0.3）---------------------------------------------------------
    def bot_seats(self):
        """坐着 bot 的座位号（升序）。"""
        return [i for i, s in enumerate(self.seats)
                if s is not None and s.is_bot]

    def human_seats(self):
        """坐着**真人**的座位号（升序）。"""
        return [i for i, s in enumerate(self.seats)
                if s is not None and not s.is_bot]

    def human_count(self):
        """房里还有几个真人。

        ★ 「房间还有没有存在的意义」只能按这个数判：一屋子 bot 谁也开不了局、
        谁也删不掉它们（命令只有房主能敲，而房主只会是真人，D2）。
        """
        return len(self.human_seats())

    def human_members(self, exclude=None):
        """房里**真人**的连接（可排除一个）。

        要「找个人来跑一段战斗逻辑」时用它，别用 `members()[0]` ——
        那一位可能是 bot，而 bot 没有账号、没有屏幕，不该替全场做判定。
        """
        return [s.conn for s in self.seats
                if s is not None and not s.is_bot
                and s.conn is not None and s.conn is not exclude]

    def bot_members(self, exclude=None):
        """房里 **bot** 的连接（`BotConn`，可排除一个）。

        M2 那两处「bot 没有加载过程，收到广播的那一刻就算它加载完」（D4）
        靠它拿人：开局的 `0x0400` 和换图的 `0x0417`。

        ★ 判据是座位上的 `is_bot`，**不是** `isinstance(conn, BotConn)`
        —— `lobby.py` 不认识 `bot.py`，导入方向是单向的（§14）。
        """
        return [s.conn for s in self.seats
                if s is not None and s.is_bot
                and s.conn is not None and s.conn is not exclude]

    def is_full(self):
        return self.free_seat() is None

    def is_playing(self):
        return self.status != SESSION_STATUS_WAITING

    def free_seat(self):
        """最小的空座位号；满了返回 ``None``。"""
        for index, seat in enumerate(self.seats):
            if seat is None:
                return index
        return None

    def seat_index_of(self, conn):
        for index, seat in enumerate(self.seats):
            if seat is not None and seat.conn is conn:
                return index
        return None

    def seat_of(self, conn):
        index = self.seat_index_of(conn)
        return None if index is None else self.seats[index]

    def members(self, exclude=None):
        """房里所有连接（可排除一个）。广播时用。"""
        return [s.conn for i, s in enumerate(self.seats)
                if s is not None and s.conn is not None and s.conn is not exclude]

    def seat_snapshots(self):
        """六个座位的 kwargs 列表，直接喂 `build_session_members`。"""
        return [seat.snapshot() if seat is not None else {"occupied": False}
                for seat in self.seats]

    # -- 分队 ---------------------------------------------------------------
    def team_layout(self):
        """本房间按哪种口径分队（`TEAM_LAYOUT_*`）。"""
        return team_layout_of(self.session_type, self.arguments)

    def item_mode(self):
        """本房间是不是道具模式（아이템전）。见 `item_mode_of` / §190。"""
        return item_mode_of(self.session_type, self.arguments)

    def default_team_for(self, seat_index):
        """新人坐进 `seat_index` 时默认分到哪一队。"""
        return default_team(seat_index, self.team_layout())

    def reassign_teams(self):
        """按当前模式把**所有**在座座位的队伍重排一遍，返回变了的座位号。

        ★ 只在**模式变了**（房主在房间里点了「组队战 / 个人战」，`0x0302`）
        的时候用。有人进房时**不能**用它 —— 那会把别人手动选的队伍冲掉。
        """
        layout = self.team_layout()
        changed = []
        for index, seat in enumerate(self.seats):
            if seat is None:
                continue
            want = default_team(index, layout)
            if seat.team != want:
                seat.team = want
                changed.append(index)
        return changed

    def clear_ready(self):
        """把所有座位的「准备好了」清掉。开一局和回房间时各调一次。

        ★ 这是**跟着客户端来的**，不是我们的发明：客户端进「加载中」那一格
        （stage 6，`LoadingStage` 构造函数 `0x46fc0f` 起的六次循环）会自己把
        `[座位+0x2e]` 全清 0（FINDINGS §165）。服务端不跟着清，下一局回到
        房间时它记的准备状态就和客户端不一样了 —— 而且没有任何包能让人
        看出来是哪边错了。
        """
        for seat in self.seats:
            if seat is not None:
                seat.ready = False

    def describe(self):
        """一行人话，给日志用。"""
        return (f"房间 #{self.room_id}「{self.title}」"
                f"type={self.session_type} map={self.map_name!r} "
                f"{self.player_count()}/{ROOM_SEAT_COUNT}人 "
                f"{'游戏中' if self.is_playing() else '待机中'}"
                f"{' 有密码' if self.password else ''}")


class LeaveResult:
    """`Lobby.leave()` 的返回值。

    离开房间要做的事不止一件，调用方需要知道全部：给谁广播、房主换成了谁、
    房间是不是空了（空了就从列表里摘掉）。
    """

    __slots__ = ("room", "seat_index", "remaining", "new_host_seat", "closed",
                 "dropped_bots")

    def __init__(self, room, seat_index, remaining, new_host_seat, closed,
                 dropped_bots=()):
        self.room = room
        self.seat_index = seat_index
        self.remaining = remaining          #: 还留在房里的连接
        self.new_host_seat = new_host_seat  #: 房主换人了就是新座位号，否则 None
        self.closed = closed                #: 房间是不是被解散了
        #: 跟着一起被摘掉的 bot 座位号（最后一个真人走时才非空）。只给日志用
        #: —— 这时房间已经解散，没有任何人需要收到座位变更广播。
        self.dropped_bots = tuple(dropped_bots)


class Lobby:
    """全部房间。线程安全。

    ★ **锁的纪律**：所有公开方法进出各拿一次锁，返回的都是**快照**
    （list / 新对象），绝不把锁带到 socket 发送那一步 —— 发包会阻塞，
    拿着大厅锁阻塞就等于全服卡死。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._rooms = {}          #: room_id -> Room
        self._by_conn = {}        #: conn -> Room
        self._next_id = 0

    # -- 建房 / 解散 ---------------------------------------------------------
    def create_room(self, conn, *, title="", map_name="", session_type=1,
                    arguments=(), password="", seat=None):
        """建一个房间，建房的人坐 0 号位。返回 `Room`。

        同一条连接重复建房时先把它从旧房间摘掉（客户端在房间里是发不出
        `0x0201` 的，但控制通道和异常路径可能造出这种状态）。
        """
        with self._lock:
            if conn is not None:
                self._leave_unlocked(conn)
            room_id = self._next_id
            self._next_id += 1
            room = Room(room_id, conn, title=title, map_name=map_name,
                        session_type=session_type, arguments=arguments,
                        password=password)
            room.seats[0] = seat if seat is not None else Seat(conn)
            room.seats[0].conn = conn
            # 队伍按座位号 + 房间模式定（§165）。建房的人固定坐 0 号。
            room.seats[0].team = room.default_team_for(0)
            room.seats[0].ready = False
            room.host_seat = 0
            self._rooms[room_id] = room
            # ★ `conn=None` 是调试通道造的「假房间」（只为在单机上验证房间列表
            #   的线格式）。它不该进 conn -> room 的索引，否则下一个假房间会把
            #   上一个的座位 0 摘掉 —— 两个假房间共用同一个 key `None`。
            if conn is not None:
                self._by_conn[conn] = room
            return room

    def close_room(self, room_id):
        """强行解散一个房间。调试通道用；正常路径靠「最后一个人走了」自动关。"""
        with self._lock:
            room = self._rooms.pop(int(room_id), None)
            if room is None:
                return None
            for seat in room.seats:
                if seat is not None and seat.conn is not None:
                    self._by_conn.pop(seat.conn, None)
            room.seats = [None] * ROOM_SEAT_COUNT
            return room

    def get(self, room_id):
        with self._lock:
            return self._rooms.get(int(room_id))

    def room_of(self, conn):
        with self._lock:
            return self._by_conn.get(conn)

    def rooms(self, game_type=None, waiting_only=False):
        """房间列表快照。

        - `game_type` 非 None 时只留匹配的（§139）；
        - `waiting_only=True` 时只留**待机中**的房间 —— 大厅左下角
          「全部 / 待机」那对按钮就是它（§170）。
        """
        with self._lock:
            found = [r for r in self._rooms.values() if not r.is_empty()]
        if game_type is not None:
            found = [r for r in found if r.game_type == int(game_type)]
        if waiting_only:
            found = [r for r in found if not r.is_playing()]
        found.sort(key=lambda r: r.room_id)
        return found

    def count(self):
        with self._lock:
            return len(self._rooms)

    # -- 加入 ---------------------------------------------------------------
    def join(self, conn, room_id, password="", seat=None):
        """尝试进房。返回 `(结果码, room|None, 座位号|None)`。

        结果码就是 `0x0202` 应答的第一个 int32，语义见 §140 —— **失败原因
        必须分开回**，全都回 3 的话玩家看到的提示是错的（同 D071 的道理）。
        """
        with self._lock:
            room = self._rooms.get(int(room_id))
            if room is None or room.is_empty():
                return MOVE_INTO_NO_SUCH_ROOM, None, None
            existing = room.seat_index_of(conn)
            if existing is not None:
                # 已经在这个房间里了（重复请求 / 客户端重发）。当成成功，
                # 免得把人踢出一个他其实待着的房间。
                return MOVE_INTO_OK, room, existing
            if room.is_playing():
                return MOVE_INTO_ALREADY_PLAYING, None, None
            # ★ 密码先于满员判：满员的房间报「密码错误」会把人带偏，
            #   但密码错的房间报「人满」同样是撒谎。两者都错时按客户端
            #   最可能的意图（他刚输了密码）先报密码。
            if room.password and str(password or "") != room.password:
                return MOVE_INTO_BAD_PASSWORD, None, None
            index = room.free_seat()
            if index is None:
                return MOVE_INTO_FULL, None, None
            self._leave_unlocked(conn)
            new_seat = seat if seat is not None else Seat(conn)
            new_seat.conn = conn
            # 进哪个座位就属于哪一队（§165）。★ 只定**新人这一格**，
            # 绝不重排整个房间 —— 那会把别人手动选的队伍冲掉。
            # 新人一律「未准备」：客户端那边这个字节也是 0，一开始就得对上。
            new_seat.team = room.default_team_for(index)
            new_seat.ready = False
            room.seats[index] = new_seat
            self._by_conn[conn] = room
            return MOVE_INTO_OK, room, index

    def quick_join(self, conn, game_type=None, seat=None):
        """`0x0205 gcpQuickJoinSession`：挑一个能进的房间进去。

        返回和 `join()` 一样的三元组。没有可进的房间时回
        `MOVE_INTO_NO_SUCH_ROOM`（客户端提示「没有符合条件的房间。」，正好对）。
        """
        for room in self.rooms(game_type):
            if room.is_playing() or room.is_full() or room.password:
                continue
            result, joined, index = self.join(conn, room.room_id, seat=seat)
            if result == MOVE_INTO_OK:
                return result, joined, index
        return MOVE_INTO_NO_SUCH_ROOM, None, None

    # -- 离开 ---------------------------------------------------------------
    def leave(self, conn):
        """把 `conn` 从它所在的房间摘掉。不在任何房间时返回 ``None``。

        断线、退房、被踢、被顶号**都走这一条**，别再写第二份。
        """
        with self._lock:
            return self._leave_unlocked(conn, want_result=True)

    def _leave_unlocked(self, conn, want_result=False):
        room = self._by_conn.pop(conn, None)
        if room is None:
            return None
        index = room.seat_index_of(conn)
        if index is None:
            return None
        room.seats[index] = None
        # ★ 最后一个**真人**走了 -> 把 bot 全摘掉，房间跟着解散（V0.3 M1）。
        #   一屋子 bot 谁也开不了局、谁也删不掉它们（命令只有房主能敲，而
        #   房主永远是真人，D2），留着就是一个永远不会消失的僵尸房间：
        #   它还会挂在大厅列表上骗人进来。判据是**真人数**，不是座位数。
        dropped_bots = ()
        if room.human_count() == 0:
            dropped_bots = self._drop_bots_unlocked(room)
        remaining = room.members()
        new_host_seat = None
        closed = False
        # ★ 判「房间空了没有」要看**座位**，不是看 `remaining`（还剩几条连接）。
        #   两者在正常游玩时等价，但调试通道造的假房间里座位的 conn 是 None，
        #   按连接数判会把还坐着人的房间当空房解散掉。
        if room.is_empty():
            self._rooms.pop(room.room_id, None)
            closed = True
        elif room.host_seat == index:
            # 房主走了 -> 转给还在的最小座位号。不转的话房间里没人能按开始，
            # 而客户端只认「房主座位号」这一个字段（V0.1 §77）。
            # ★ **跳过 bot 座**（V0.3 D2）：房主是唯一能开局、能敲 bot 命令的
            #   人，转给 bot 等于房间彻底死掉。判据是「这格是不是 bot」，
            #   **不是**「conn 是不是 None」—— 假房间的座位 conn 也是 None。
            for candidate, seat in enumerate(room.seats):
                if seat is not None and not seat.is_bot:
                    new_host_seat = candidate
                    room.host_seat = candidate
                    room.host_conn = seat.conn
                    break
        if not want_result:
            return None
        return LeaveResult(room, index, remaining, new_host_seat, closed,
                           dropped_bots)

    def _drop_bots_unlocked(self, room):
        """把房里所有 bot 座位摘掉，返回被摘掉的座位号。"""
        dropped = []
        for index, seat in enumerate(room.seats):
            if seat is None or not seat.is_bot:
                continue
            room.seats[index] = None
            if seat.conn is not None:
                self._by_conn.pop(seat.conn, None)
            dropped.append(index)
        return tuple(dropped)

    # -- bot（V0.3）---------------------------------------------------------
    def add_bot(self, room, seat):
        """把一个 bot 座位放进最小空座。返回座位号；满座返回 ``None``。

        队伍按座位号 + 房间模式定，和真人进房走的是同一条 `default_team_for`
        —— 组队房里座位号奇偶交替 1/2，所以「从最小空座往下填」天然保持
        两队平衡（客户端要求两队人数相等才让开局，V0.3 §8）。
        """
        with self._lock:
            index = room.free_seat()
            if index is None:
                return None
            seat.is_bot = True
            seat.team = room.default_team_for(index)
            seat.ready = False
            room.seats[index] = seat
            # bot 也进 conn -> room 索引：`Conn.lobby_room()` / `battle_members()`
            # 一大堆路径都靠它找人（D1 选了「假连接」而不是 `conn=None`）。
            if seat.conn is not None:
                self._by_conn[seat.conn] = room
            return index

    def remove_bot(self, room, seat_index):
        """摘掉一个 bot 座位。返回被摘掉的 `Seat`；那格不是 bot 就返回 ``None``。

        ★ 「那格不是 bot」包括「空着」和「坐着真人」两种，调用方要按 ``None``
        给出具体原因 —— 悄悄成功地删掉一个真人是绝对不允许的。
        """
        with self._lock:
            if not 0 <= int(seat_index) < ROOM_SEAT_COUNT:
                return None
            seat = room.seats[int(seat_index)]
            if seat is None or not seat.is_bot:
                return None
            room.seats[int(seat_index)] = None
            if seat.conn is not None:
                self._by_conn.pop(seat.conn, None)
            return seat

    def kick(self, room, seat_index):
        """房主踢人。返回被踢掉的 `LeaveResult`，座位空着就返回 ``None``。"""
        with self._lock:
            if not 0 <= int(seat_index) < ROOM_SEAT_COUNT:
                return None
            seat = room.seats[int(seat_index)]
            if seat is None or seat.conn is None:
                return None
            return self._leave_unlocked(seat.conn, want_result=True)

    # -- 房间状态 -----------------------------------------------------------
    def update_room(self, room, *, title=None, map_name=None,
                    session_type=None, arguments=None, password=None,
                    status=None, random_map=None):
        """改房间参数（`0x0302 gcpChangeSession` 选完地图之后）。"""
        with self._lock:
            if title is not None:
                room.title = title
            if map_name is not None:
                room.map_name = map_name
            if random_map is not None:
                room.random_map = bool(random_map)
            if session_type is not None:
                room.session_type = int(session_type)
            if arguments is not None:
                room.arguments = tuple(arguments)
            if password is not None:
                room.password = password
            if status is not None:
                room.status = int(status)
            return room

    def reset(self):
        """清空。只给测试和「服务端重启」用。"""
        with self._lock:
            self._rooms.clear()
            self._by_conn.clear()
            self._next_id = 0
