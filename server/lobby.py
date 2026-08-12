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

#: 一个房间怎么分队。★ **队伍号不只是好看** —— 客户端在
#: `Character::vf(+0xb8)`（`0x4fedfc` / `0x4ffec3`）里比双方的队伍号，
#: **相同就直接不结算伤害**，而且这一处**不分游戏模式**。
#: 所以「所有人都填 0」（V0.2 会话 10 之前的行为）等于全场互相免疫。
#:
#: `TEAMS` 组队战：座位号奇偶交替 1 / 2。客户端自己填 Dummy 座位就是这条
#:        （`0x468952`：`idiv 2 ; inc dl ; mov [slot+8], dl`）。
#:        ⚠ 这个模式下客户端**要求两队人数相等**才让开局
#:        （`0x468495` 数 1 队和 2 队，不等就回错误码 3 ->「两组人数不相同。
#:        请调整人数。」），所以 3 人 / 5 人的组队战本来就开不起来。
#: `FREE` 个人战：每人一队（座位号 + 1），否则谁都打不动谁。
#: `COOP` 闯关等：全在 1 队 —— 队友之间不该有伤害。
TEAM_LAYOUT_TEAMS = "teams"
TEAM_LAYOUT_FREE = "free"
TEAM_LAYOUT_COOP = "coop"

#: 房间描述符 type == 1（普通对战）时，`arguments[0] == 1` = 组队战。
#: 客户端的 `0x409df1` 对 type 1 返回的就是 `arguments[0]`，全部「要不要
#: 分队」的判断（房间里的站位、名牌颜色、队伍人数检查）都读它。
SESSION_TYPE_NORMAL = 1
NORMAL_ARGUMENT_TEAM_MODE = 1


def team_layout_of(session_type, arguments):
    """这个房间该按哪种口径分队。见 `TEAM_LAYOUT_*`。"""
    if int(session_type) != SESSION_TYPE_NORMAL:
        return TEAM_LAYOUT_COOP
    first = arguments[0] if arguments else 0
    return (TEAM_LAYOUT_TEAMS if int(first) == NORMAL_ARGUMENT_TEAM_MODE
            else TEAM_LAYOUT_FREE)


def default_team(seat_index, layout=TEAM_LAYOUT_TEAMS):
    """一个座位在某种口径下默认属于哪一队。"""
    index = int(seat_index)
    if layout == TEAM_LAYOUT_TEAMS:
        return (index % 2) + 1
    if layout == TEAM_LAYOUT_FREE:
        return index + 1
    return TEAM_A


class Seat:
    """房间里的一个座位。

    昵称 / 等级 / 角色 id / 队伍 / 准备状态存的是**快照**：`0x0300` 和 `0x0301`
    要把它们发给房里每一个人，而别人的连接查不到你的存档。换角色
    （`0x0301` action 4）、变更队伍、按「游戏准备」和重新登录时由 `gameserver`
    调 `update()` 刷新。
    """

    __slots__ = ("conn", "username", "nickname", "level", "character_id",
                 "team", "ready")

    def __init__(self, conn, username="", nickname="", level=1, character_id=0,
                 team=TEAM_NONE, ready=False):
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

    # -- 查询 ---------------------------------------------------------------
    @property
    def game_type(self):
        return SESSION_TYPE_GAME_TYPES.get(self.session_type, 1)

    def player_count(self):
        return sum(1 for s in self.seats if s is not None)

    def is_empty(self):
        return self.player_count() == 0

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

    __slots__ = ("room", "seat_index", "remaining", "new_host_seat", "closed")

    def __init__(self, room, seat_index, remaining, new_host_seat, closed):
        self.room = room
        self.seat_index = seat_index
        self.remaining = remaining          #: 还留在房里的连接
        self.new_host_seat = new_host_seat  #: 房主换人了就是新座位号，否则 None
        self.closed = closed                #: 房间是不是被解散了


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

    def rooms(self, game_type=None):
        """房间列表快照。`game_type` 非 None 时只留匹配的（§139）。"""
        with self._lock:
            found = [r for r in self._rooms.values() if not r.is_empty()]
        if game_type is not None:
            found = [r for r in found if r.game_type == int(game_type)]
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
            for candidate, seat in enumerate(room.seats):
                if seat is not None:
                    new_host_seat = candidate
                    room.host_seat = candidate
                    room.host_conn = seat.conn
                    break
        if not want_result:
            return None
        return LeaveResult(room, index, remaining, new_host_seat, closed)

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
                    status=None):
        """改房间参数（`0x0302 gcpChangeSession` 选完地图之后）。"""
        with self._lock:
            if title is not None:
                room.title = title
            if map_name is not None:
                room.map_name = map_name
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
