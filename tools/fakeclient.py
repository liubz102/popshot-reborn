#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fakeclient.py —— 用 Python 冒充第二个游戏客户端（大厅部分）

**为什么需要它**：房间里的「另一个人」相关的行为（进房广播、离开广播、
房主转移、踢人）在一台电脑上根本造不出来 —— `gs_ctl.py fakeroom` 造的假房间
没有连接，`gs_ctl.py raw` 只能手搓单个包。这个脚本走的是**真的 TCP + 真的
认证 + 真的票据**，服务端完全把它当成一个正经玩家，于是：

    真客户端能看到它进房 / 说话 / 离开 / 被踢（用 tools/screenshot.py 看）
    它也能把服务端广播给它的包逐字节打出来（真客户端那边看不见字节）

它**不渲染任何东西**，只管收发包。战斗类的包也能发（`ready` / `loaded` /
`die` / `respawn` / `drop` / `pickup` / `score` / `endquest`），但它没有关卡、
没有角色，只是把字节发出去 —— 用来验**服务端的广播和仲裁**对不对
（J.3：一件东西只能被一个人捡到、死亡广播给全房间、结算每座位一份）。

用法（命令是一串 token，从左往右顺序执行）：

    python tools/fakeclient.py <账号> <密码> join 1 sleep 5 leave sleep 3
    python tools/fakeclient.py bob pw456 join 1 shot 5964 before.png \\
        leave sleep 2 shot 5964 after.png
    python tools/fakeclient.py bob pw456 join 1 chat "hello" hold 60

命令表：

    create [标题] [类型] [参数,参数,参数]
                      发 0x0201 建一个房间（自己当房主，座位 0）。
                      参数不给就用默认；type 1（普通对战）的三个参数是
                      **组队 / 游戏模式 / 道具模式**（§190），
                      所以 `create 房间 1 0,0,1` = 个人战 + **道具模式**
    join <房间号>     发 0x0202 gcpReqMoveInto，并等应答（打印 result / 座位号）
    leave             发 0x0203 gcpReqLeaveSession
    chat <文本>       发 0x0305 gcpSendChatMsg
    team <1|2> [座位] 「变更队伍」：改一份座位副本的队伍号再发 0x0301（§165）。
                      不给座位号就改自己那一格（房主才动得了别人的）
    roomready [0|1]   「游戏准备 / READY」：发 0x030e（§165）。
                      ★ 和下面那个 `ready`（0x0402 开局握手）不是一回事
    seats             把服务端最后一次告诉我的六个座位打出来（含队伍/准备状态）
    rooms [0|1] [类型] 发 0x0200 要房间列表。第一个参数 = 大厅左下角那对按钮：
                      **1 = 只看待机（客户端进大厅时发的就是 1）**、0 = 全部（§170）
    users [0|1] [页]  发 0x020d 要大厅右侧玩家列表。**1 = 待机玩家（默认）**、
                      0 = 推荐对手（§169）。收到的 0x0212 会解成「谁 / P还是W /
                      等级 / 天梯」
    peer [序列号] [局号]
                      发一发 0x040e = 玩家间同步数据（12 字节 UdpPacket 头 +
                      3 字节 body，§149~§151）。用来验服务端有没有原样转成 0x040f。
                      ★ 第二个参数给**别人不一样的局号**，就能验服务端有没有
                      按收件人的号重新盖章（bug调查/8_2「其他人都不会动」）
    rpeer [序列号] [局号]
                      同上，但走**原版 TCP 中继**（rcp opcode 3）。要先连上中继
    waitrelay [秒]    等中继连上（服务端回 0x0210 之后才会有），超时就报错退出

  ── 战斗（J.3）。全部按「真客户端会发的线格式」组包 ──
    ready             发 0x0402（房主按 F5 开局；连发两次走完握手）
    loaded            发 0x0403「关卡加载完了」（所有人都报了才一起进 stage 7）
    die [次数] [座位] 发 0x0408「我 HP 归零了」，句柄按 座位*100000+100001 算。
                      ★ 第二个参数给**别人的**座位 = 造一发「幽灵死亡上报」
                      （替别人报死，bug调查/8）；服务端应当不广播
    respawn           发 0x0413「我要重生」
    drop [物件id]     发 0x0406 gcpCreateItem，等服务端回 0x0404 派句柄
    pickup <句柄>     发 0x0407 gcpGetItem「我踩到这件了」（十六进制也行）
    useitem [槽位]    发 0x040c「按 Ctrl 用道具」（真客户端**恒发 0**，§194）。
                      服务端应当回一发 0x040c（只给自己，扣掉那一格）+
                      广播一发 0x040a（道具效果作用到那个座位）
    endattr [属性号]  发 0x040d「我身上这个效果结束了」（默认 6 = 三重射击，§200）。
                      服务端应当把它转给房里**其他人**（自己不该收到）
    score <分数>      发 0x0410 gcpUpdateQuestScore（**累计**分数）
    cleared           发 0x0417 gcpMarkQuestSuccess(1)「这一关打通了」
    nextmap <名字>    发 0x0411 gcpReqChangeToNextMap
    maploaded         发 0x0412「新图加载完了」
    endquest          发 0x040f gcpEndQuest（触发结算）
    resultdone        发 0x0405「结算看完了」（回房间）

    sleep <秒>        原地等（收包线程照常在跑）
    hold <秒>         同 sleep，语义上表示「挂在这儿让人看」
    shot <pid> <png>  调 tools/screenshot.py 抓一张**真客户端**的截图
    quit              主动断开（不发 0x0203，模拟拔网线）

★ 收到的每一帧都会打印；`0x0301`（座位变更）会额外把 action 和座位号解出来，
  离开广播到底发的是哪个 action 一眼就能看见（§147）。
  `0x0410`（同步开关）和 `0x040f`（转发来的玩家数据）也会解出来。

★★ 收到 `0x0210 gspJoinRelay` 会**像真客户端一样**去连原版 TCP 中继
   （§157：连上就发 rcpRegister，收到 opcode 1 回 ping、收到 opcode 2 重报身份）。
   收到 `0x0211` 就把那条连接拆掉。中继上收到的每一帧同样逐字节打印。
"""
import os
import struct
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

import protocol as P                                     # noqa: E402
import relayserver as R                                  # noqa: E402
from simple import SimpleCipher                          # noqa: E402
from gameserver import (                                 # noqa: E402
    CLIENT_VERSION, CREATE_ITEM_FORMAT, DEATH_REPORT_FORMAT,
    DESCRIPTOR_SENT_ARGUMENT_COUNTS,
    CHAR_ATTR_NAMES,
    GCP_NAMES, MAGIC_CTRL, MAGIC_GAME, OP_CHAT, OP_COUNT_GAME_READY,
    OP_CREATED_ITEM, OP_CREATE_ITEM, OP_END_QUEST, OP_GET_ITEM,
    OP_GRANT_ITEM, OP_ITEM_EFFECT, OP_REMOVE_CHAR_ATTR, OP_USE_ITEM,
    ITEM_NAMES,
    OP_JOIN_RELAY, OP_LEAVE_RELAY, OP_LEAVE_RESULT, OP_LOADING_DONE,
    OP_MAP_LOADING_DONE, OP_MARK_QUEST_SUCCESS, OP_PICKED_ITEM,
    OP_REP_GAME_RESULT, OP_REP_QUEST_SCORE, OP_REPORT_HP_ZERO,
    OP_REQ_CHANGE_TO_NEXT_MAP, OP_REQ_RESPAWN, OP_START_TCP_RELAY,
    OP_LEAVE_SESSION, OP_MOVE_INTO_SESSION, OP_PEER_DATA_DOWN,
    OP_PEER_DATA_UP, OP_SEAT_READY, OP_SESSION_MEMBER_UPDATE,
    OP_SESSION_MEMBERS, OP_TOGGLE_PEER_RELAY, OP_UPDATE_QUEST_SCORE,
    ROOM_SEAT_COUNT, Reader, build_game, build_session_slot,
    describe_peer_header, parse_session_slot, take_frame, w_i32, w_wstr,
)

#: 玩家角色的对象句柄 = **座位 × 100000 + 100001**（客户端 `0x405f02`
#: 写死的公式，§161）。`0x0408` 死亡上报里的那个句柄就是它，六台机器上
#: 算出来的值一模一样 —— 假客户端也照这个公式算，服务端才认得出是谁死了。
CHARACTER_HANDLE_STEP = 100000
CHARACTER_HANDLE_BASE = 100001


def character_handle(seat):
    return seat * CHARACTER_HANDLE_STEP + CHARACTER_HANDLE_BASE


def build_udp_packet(seat, sequence=1, game_id=0, body=b"\xde\xad\xbe"):
    """一份 `UdpPacket`：12 字节头 + body（§151）。

    头 = magic / 发送方座位 / 目标座位（0xff 广播）/ ? / **局号** /
    校验和 / 序列号 / 内层 opcode。

    ★ `game_id`（头 `+4`）是真客户端**每台自己数的**那个计数器，收方拿它
    和自己的 `[GameSession+0x3c]` 比，不等就整包丢掉（`0x4078c4`）。
    在同一个房间里多打一局就会分叉 —— 这就是 bug调查/8_2「其他人都不会动」。
    服务端现在转发时会按收件人自己的号重新盖章，拿这个参数就能验。
    """
    return (struct.pack("<BbbB", 0xFF, seat, -1, 0)
            + struct.pack("<HHHH", int(game_id), 0x1234, int(sequence), 0x0102)
            + bytes(body))

#: 连哪几个端口。默认就是客户端写死的那两个；环境变量 `POPSHOT_AUTH_PORT` /
#: `POPSHOT_GAME_PORT` 可以改，**专门是为了「不去动用户自己那份正在跑的服务端」**
#: —— 验证时另起一套端口（例如 47811 / 27999 / 27998）就不会撞车。
#: 中继端口不用在这里配：它是服务端在 `0x0210` 里现告诉我们的。
AUTH_PORT = int(os.environ.get("POPSHOT_AUTH_PORT") or 47611)
GAME_PORT = int(os.environ.get("POPSHOT_GAME_PORT") or 27799)

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

_started = time.monotonic()


def log(text):
    print(f"[{time.monotonic() - _started:6.2f}s] {text}", flush=True)


# ---------------------------------------------------------------- 认证服
def get_ticket(host, user, password, timeout=10.0):
    """走一遍 47611 的登录，返回票据（认证服放在应答的第二个字符串里，§123）。"""
    import socket
    with socket.create_connection((host, AUTH_PORT), timeout=timeout) as sock:
        sock.sendall(P.pack(P.Frame(P.OPCODE_LOGIN,
                                    P.build_login(user, password))))
        buf = bytearray()
        while True:
            need = P.frame_len(buf)
            if need is not None and len(buf) >= need:
                break
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("认证服在回完整应答之前就断开了")
            buf += chunk
        frame = P.unpack(bytes(buf[:P.frame_len(buf)]))
    result, message, ticket, _a, _b, _c = P.parse_login_reply(frame.payload)
    if result != 0 or not ticket:
        raise RuntimeError(f"认证失败：result={result} 说明={message!r}")
    log(f"认证通过：票据 {ticket[:8]}… 说明={message!r}")
    return ticket


# ---------------------------------------------------------------- 原版中继
class RelayLink:
    """一条到原版 TCP 中继的连接，**照着真客户端的行为演**（§157）。

    真客户端拿到 `0x0210` 就 `new RelayConnection` 连过去，
    `OnConnected`（`0x54bdc2`）第一件事是发 `rcpRegister`；
    之后收到 opcode 1 回一发 opcode 1、收到 opcode 2 就重报一次身份。
    **没有明文版本号开场白** —— 那是游戏服连接独有的（§156）。
    """

    def __init__(self, host, port, auth):
        import socket
        self.auth = tuple(auth)
        self.sock = socket.create_connection((host, port), timeout=10.0)
        self.cin = SimpleCipher.server_to_client()
        self.cout = SimpleCipher.client_to_server()
        self.buf = bytearray()
        self.alive = True
        self.lock = threading.Lock()
        self.data_in = []
        threading.Thread(target=self._recv_loop, daemon=True).start()
        log(f"[中继] 已连上 {host}:{port}，发 rcpRegister{self.auth}")
        self.send(R.RCP_REGISTER, struct.pack("<iii", *self.auth))

    def send(self, opcode, payload=b""):
        with self.lock:
            self.sock.sendall(self.cout.encrypt(R.build_rcp(opcode, payload)))

    def send_peer(self, blob):
        log(f"[中继] → 发 rcp opcode 3（数据）{len(blob)} 字节")
        self.send(R.RCP_DATA_UP, blob)

    def _recv_loop(self):
        self.sock.settimeout(1.0)
        while self.alive:
            try:
                data = self.sock.recv(8192)
            except TimeoutError:
                continue
            except OSError:
                break
            if not data:
                log("[中继] !! 对端关闭 —— 真客户端遇到这个会自己退出房间（§158）")
                break
            self.buf += self.cin.decrypt(data)
            while True:
                try:
                    got = R.take_rcp(self.buf)
                except ValueError as error:
                    log(f"[中继] !! {error}")
                    self.buf.clear()
                    break
                if got is None:
                    break
                opcode, payload, size = got
                del self.buf[:size]
                self._on_frame(opcode, payload)

    def _on_frame(self, opcode, payload):
        if opcode == R.RCP_DATA_DOWN:
            self.data_in.append(payload)
            log(f"[中继] ← 收 opcode 0（数据）{len(payload)} 字节\n"
                f"{describe_peer_header(payload)}")
        elif opcode == R.RCP_PING:
            log("[中继] ← 收 opcode 1（ping），回一发")
            self.send(R.RCP_REP_PING)
        elif opcode == R.RCP_WHO_ARE_YOU:
            log("[中继] ← 收 opcode 2（报身份），重发 rcpRegister")
            self.send(R.RCP_REGISTER, struct.pack("<iii", *self.auth))
        else:
            log(f"[中继] ← 收 opcode {opcode}（不认识）{len(payload)} 字节")

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- 游戏服
class FakeClient:
    """一条到 27799 的连接。收包在后台线程里跑，发包在主线程。"""

    def __init__(self, host, ticket):
        import socket
        self.sock = socket.create_connection((host, GAME_PORT), timeout=10.0)
        self.cin = SimpleCipher.server_to_client()    # 服务端发过来的那一半
        self.cout = SimpleCipher.client_to_server()   # 我发出去的那一半
        self.buf = bytearray()
        self.alive = True
        self.my_seat = None
        self.room_id = None
        #: 服务端最后一次告诉我的六个座位（`0x0300` 全量 / `0x0301` 增量解出来）。
        #: `team` 命令要把**没变的字段原样发回去**，不然服务端会把它当成换角色
        #: （§165 的判据就是「角色 id 没变、队伍变了」）。
        self.seats = [None] * ROOM_SEAT_COUNT
        self.relay = None                             # RelayLink（收到 0x0210 才有）
        self.lock = threading.Lock()
        self.reader = threading.Thread(target=self._recv_loop, daemon=True)
        self.reader.start()
        # 客户端连上来的第一件事是报版本号（明文 int32，服务端回一个 0xFE 控制帧）
        self.send_raw(w_i32(CLIENT_VERSION))
        self.send_game(0x0100, w_wstr(ticket))         # gcpReqLogin

    # -- 发
    def send_raw(self, plain):
        with self.lock:
            self.sock.sendall(self.cout.encrypt(plain))

    def send_game(self, opcode, payload=b""):
        name = GCP_NAMES.get(opcode, "?")
        log(f"→ 发 0x{opcode:04x} ({name}) {len(payload)} 字节")
        self.send_raw(build_game(opcode, payload))

    # -- 收
    def _recv_loop(self):
        self.sock.settimeout(1.0)
        while self.alive:
            try:
                data = self.sock.recv(8192)
            except TimeoutError:
                continue
            except OSError:
                break
            if not data:
                log("!! 服务端关闭了连接")
                break
            self.buf += self.cin.decrypt(data)
            while True:
                got = take_frame(self.buf)
                if got is None:
                    break
                kind, opcode, payload, n = got
                del self.buf[:n]
                self._on_frame(kind, opcode, payload)

    def _on_frame(self, kind, opcode, payload):
        if kind == "ctrl":
            log(f"← 收 0xFE 控制帧 {payload.hex(' ')}")
            return
        note = describe(opcode, payload)
        log(f"← 收 0x{opcode:04x} {len(payload)} 字节{note}")
        if opcode == OP_MOVE_INTO_SESSION and len(payload) >= 12:
            result, room_id, seat = struct.unpack_from("<iii", payload)
            if result == 0:
                self.room_id, self.my_seat = room_id, seat
        elif opcode == OP_SESSION_MEMBERS:
            self._remember_snapshot(payload)
        elif opcode == OP_SESSION_MEMBER_UPDATE:
            self._remember_seat_update(payload)
        elif opcode == OP_JOIN_RELAY and len(payload) >= 18:
            self._join_relay(payload)
        elif opcode == OP_LEAVE_RELAY:
            if self.relay is not None:
                log("[中继] 收到 0x0211 —— 拆掉中继连接（走析构，不是断线）")
                self.relay.close()
                self.relay = None

    # -- 座位表（`team` / `roomready` 要用）--------------------------------
    def _remember_snapshot(self, payload):
        """`0x0300` 全量座位快照 -> `self.seats`。"""
        try:
            reader = Reader(payload)
            reader.i32()                                 # 房主座位号
            reader.i32()                                 # ?
            seats = [parse_session_slot(reader) for _ in range(ROOM_SEAT_COUNT)]
        except Exception as error:                       # noqa: BLE001
            log(f"  （0x0300 解不开：{error}）")
            return
        self.seats = seats
        log("  座位表：" + self.describe_seats())

    def _remember_seat_update(self, payload):
        """`0x0301` 增量 -> 更新一格。"""
        try:
            reader = Reader(payload[1:])                 # 跳过 action 那一字节
            seat_index = reader.i32()
            slot = parse_session_slot(reader)
        except Exception as error:                       # noqa: BLE001
            log(f"  （0x0301 解不开：{error}）")
            return
        if 0 <= seat_index < ROOM_SEAT_COUNT:
            self.seats[seat_index] = slot
            log(f"  座位 {seat_index} -> {describe_slot(slot)}")

    def describe_seats(self):
        return " | ".join(
            f"{i}:{describe_slot(s)}"
            for i, s in enumerate(self.seats)
            if s is not None and s.get("occupied"))

    def my_slot(self):
        if self.my_seat is None or self.seats[self.my_seat] is None:
            return None
        return self.seats[self.my_seat]

    def _join_relay(self, payload):
        import socket
        host = socket.inet_ntoa(payload[:4])
        port = struct.unpack_from("<H", payload, 4)[0]
        auth = struct.unpack_from("<iii", payload, 6)
        if self.relay is not None:
            # ★ 真客户端在这里会**无条件**新建一条并覆盖全局指针，旧的变孤儿
            #   （§159）。假客户端照着演，但要把这件事喊出来 ——
            #   服务端要是重发了 `0x0210`，这行日志就是证据。
            log("[中继] !! 又收到一发 0x0210 —— 服务端重发了，§159 说这是定时炸弹")
            self.relay.close()
        try:
            self.relay = RelayLink(host, port, auth)
        except OSError as error:
            log(f"[中继] !! 连不上 {host}:{port} —— {error}；"
                f"真客户端遇到这个会自己退出房间（§158）")
            self.relay = None

    def close(self):
        self.alive = False
        if self.relay is not None:
            self.relay.close()
        try:
            self.sock.close()
        except OSError:
            pass


def describe_slot(slot):
    """一个座位的一行人话（队伍 / 准备状态是 §165 之后才有名字的）。"""
    if not slot or not slot.get("occupied"):
        return "空"
    return (f"{slot.get('nickname')!r} Lv.{slot.get('level')} "
            f"角色={slot.get('character_id')} "
            f"队伍={slot.get('team')}"
            f"{' 已准备' if slot.get('ready') else ''}")


def describe(opcode, payload):
    """把最关心的几个包解出人能读的一行 —— 其余的只报长度。"""
    try:
        if opcode == OP_SESSION_MEMBER_UPDATE and len(payload) >= 5:
            action = payload[0]
            seat = struct.unpack_from("<i", payload, 1)[0]
            what = {0: "加入(建模型)", 1: "离开(销毁模型)", 2: "离开(销毁模型)",
                    3: "重建模型（换队伍/准备状态走这条）", 4: "换角色"}.get(
                        action, "未知")
            return f"  ★ 座位变更 action={action}（{what}）座位={seat}"
        if opcode == OP_SESSION_MEMBERS and len(payload) >= 8:
            return f"  房主座位={struct.unpack_from('<i', payload)[0]}"
        if opcode == OP_MOVE_INTO_SESSION and len(payload) >= 12:
            result, room_id, seat = struct.unpack_from("<iii", payload)
            return f"  加入结果={result} 房间={room_id} 我的座位={seat}"
        if opcode == 0x0100 and len(payload) >= 4:
            result = struct.unpack_from("<i", payload)[0]
            what = {0: "成功",
                    1: "客户端弹「请重新确认帐号和密码。」",
                    2: "客户端弹「现有连接已断开。请重新尝试连接。」",
                    3: "客户端弹「在无法连接的地方尝试了连接。」"}.get(
                        result, "客户端不认识这个码")
            return f"  ★ gspRepLogin result={result}（{what}）"
        if opcode == 0x0200:
            reader = Reader(payload)
            rooms = []
            for _ in range(reader.u16()):
                status = reader.i32()
                title = reader.wstr()
                here = reader.i32()
                reader.wstr(), reader.i32()          # 地图名 / option
                session_type = reader.i32()
                reader.take(4 * DESCRIPTOR_SENT_ARGUMENT_COUNTS.get(
                    session_type, 3))
                room_id = reader.u16()
                cap = reader.take(1)[0]
                reader.i32()
                rooms.append(f"#{room_id} {title!r} "
                             f"{'游戏中' if status != 2 else '待机中'} "
                             f"({here}/{cap}) type={session_type}")
            return "  ★ 房间列表：" + ("；".join(rooms) if rooms else "（空）")
        if opcode == 0x0212:
            reader = Reader(payload)
            page, page_size = reader.u16(), reader.u16()
            flag = reader.take(1)[0]
            users = []
            for _ in range(reader.i32()):
                name = reader.wstr()
                playing, level, ladder = (reader.i32(), reader.i32(),
                                          reader.i32())
                users.append(f"{name}({'游戏中 P' if playing else '待机 W'} "
                             f"Lv.{level} 天梯{ladder})")
            return (f"  ★ 玩家列表（第 {page} 页 每页 {page_size} "
                    f"过滤={flag}{'=待机玩家' if flag else '=推荐对手'}）："
                    + ("、".join(users) if users else "（空）"))
        if opcode == OP_CHAT:
            reader = Reader(payload)
            reader.u16()
            sender, text = reader.wstr(), reader.wstr()
            return f"  聊天 {sender!r}: {text!r}" if sender else f"  系统提示 {text!r}"
        if opcode == OP_TOGGLE_PEER_RELAY and len(payload) >= 4:
            on = struct.unpack_from("<i", payload)[0]
            return (f"  ★ 玩家间同步开关 = {on}"
                    f"（{'开，之后要发 0x040e' if on else '关'}）")
        if opcode == OP_PEER_DATA_DOWN:
            return "  ★ 转发来的玩家数据\n" + describe_peer_header(payload)
        # ---- 战斗（J.3）：广播回来的每一发都解成一行，好逐字节对广播行为 ----
        if opcode == 0x0406 and len(payload) >= 10:
            handle, seat, arg, deaths = struct.unpack_from("<IBBi", payload)
            who = (f"座位 {seat}" if seat < 6 else f"怪物(座位={seat - 256})")
            return (f"  ★ 死亡广播 {who} 句柄=0x{handle:08x} "
                    f"凶手={arg} 死亡次数 -> {deaths}")
        if opcode == 0x0419 and len(payload) >= 16:
            cid, x, y, spawn = struct.unpack_from("<iiii", payload)
            return f"  ★ 重生 座位={cid} 坐标=({x}, {y}) 重生点={spawn}"
        if opcode == OP_CREATED_ITEM and len(payload) >= 36:
            handle = struct.unpack_from("<I", payload)[0]
            item_id, x, y = struct.unpack_from("<iff", payload, 4)
            return (f"  ★ 掉落物落地 句柄=0x{handle:08x} 物件={item_id} "
                    f"@ ({x:.0f}, {y:.0f})")
        if opcode == OP_PICKED_ITEM and len(payload) >= 8:
            seat, handle = struct.unpack_from("<iI", payload)
            return f"  ★ 拾取放行 座位={seat} 句柄=0x{handle:08x}"
        if opcode == OP_GRANT_ITEM and len(payload) >= 4:
            # 0x040b：往**收包这台机器上的本地玩家**的道具槽里塞一件（§194）。
            item_id = struct.unpack_from("<i", payload)[0]
            return (f"  ★ 进我的道具槽 物件={item_id} "
                    f"{ITEM_NAMES.get(item_id, '未知物件')}")
        if opcode == OP_USE_ITEM and len(payload) >= 4:
            slot = struct.unpack_from("<i", payload)[0]
            return f"  ★ 我的道具槽第 {slot} 格被拿掉（用掉了）"
        if opcode == OP_REMOVE_CHAR_ATTR and len(payload) >= 8:
            # 0x040d：某个座位身上的道具效果结束了（§200）。真客户端收到
            # 会拆掉那个属性对应的模型 / 特效；自己那一发它直接丢掉。
            seat, attr = struct.unpack_from("<ii", payload)
            return (f"  ★ 效果结束 座位={seat} 属性={attr} "
                    f"{CHAR_ATTR_NAMES.get(attr, '未知属性')}")
        if opcode == OP_ITEM_EFFECT and len(payload) >= 16:
            seat, arg3, item_id, arg2 = struct.unpack_from("<iiii", payload)
            return (f"  ★ 道具效果 座位={seat} 物件={item_id} "
                    f"{ITEM_NAMES.get(item_id, '未知物件')} "
                    f"(arg2={arg2} arg3={arg3})")
        if opcode == OP_REP_QUEST_SCORE and len(payload) >= 8:
            seat, score = struct.unpack_from("<ii", payload)
            return f"  ★ 分数 座位={seat} -> {score}"
        if opcode == OP_REP_GAME_RESULT and len(payload) >= 56:
            seat = struct.unpack_from("<i", payload)[0]
            count = struct.unpack_from("<i", payload, 52)[0]
            tail = list(struct.unpack_from(f"<{count}i", payload, 56))
            return (f"  ★ 结算数据 座位={seat} 名次表={tail}"
                    f"（1=完成/胜 0=未完成 -1=负）")
        if opcode == 0x0411 and len(payload) >= 8:
            seat, ok = struct.unpack_from("<ii", payload)
            return f"  ★ 关卡结束 座位={seat} success={ok}"
        if opcode == 0x0414 and len(payload) >= 8:
            # gspChangeControllerSlot（§180）：怪 / 刷怪点的模拟权换人。
            old, new = struct.unpack_from("<ii", payload)
            return (f"  ★ 控制权交接 座位 {old} -> 座位 {new}"
                    f"（怪 / 刷怪点改由座位 {new} 那台模拟）")
    except Exception:                      # 解不动就算了，别把收包线程搞死
        pass
    return ""


# ---------------------------------------------------------------- 命令
def _isnum(token):
    try:
        float(token)
    except ValueError:
        return False
    return True


def run_script(client, tokens):
    while tokens:
        cmd = tokens.pop(0)
        if cmd == "create":
            title = tokens.pop(0) if tokens and not tokens[0].isdigit() else "假房间"
            session_type = int(tokens.pop(0)) if tokens and tokens[0].isdigit() else 2
            # 0x0201 的载荷：三个字符串 + int32 + 描述符（V0.1 §69 / §137）。
            # 建房那一刻**地图名必须留空**，真地图名由随后的 0x0302 补。
            # ★ 描述符的参数**个数按类型定**（type 2 是 2 个、type 1/5/6 是
            #   3 个），个数发错服务端解不出来，房间根本建不起来。
            count = DESCRIPTOR_SENT_ARGUMENT_COUNTS[session_type]
            # 描述符参数可以显式给：`create 房间 1 0,0,1` = 个人战 + 道具模式
            # （type 1 的三个参数是「组队 / 游戏模式 / 道具模式」，§190）。
            if tokens and "," in tokens[0]:
                arguments = tuple(int(v) for v in tokens.pop(0).split(","))
            else:
                arguments = (3, 1, 0)[:count]
            if len(arguments) != count:
                raise SystemExit(f"type {session_type} 的描述符要 {count} 个参数，"
                                 f"给了 {len(arguments)} 个")
            client.send_game(0x0201,
                             w_wstr(title) + w_wstr("") + w_wstr("")
                             + w_i32(0) + w_i32(session_type)
                             + b"".join(w_i32(v) for v in arguments))
            client.my_seat, client.room_id = 0, None
            time.sleep(0.5)
            log(f"已建房「{title}」type={session_type} args={arguments}"
                "（我是房主，座位 0）")
        elif cmd == "rooms":
            # 0x0200 房间列表请求（12 字节，§139 / §170）。第 4 个字段就是
            # 大厅左下角「全部 / 待机」那对按钮：0 = 全部，1 = 只看待机。
            waiting = int(tokens.pop(0)) if tokens and tokens[0].isdigit() else 1
            game_type = int(tokens.pop(0)) if tokens and tokens[0].isdigit() else 2
            client.send_game(0x0200,
                             struct.pack("<HHH", 0, 0, 10)
                             + struct.pack("<BB", waiting, 0)
                             + w_i32(game_type))
            log(f"已发 0x0200 房间列表请求"
                f"（{'只看待机' if waiting else '全部'}，游戏类型={game_type}）")
            time.sleep(0.4)
        elif cmd == "users":
            # 0x020d 玩家列表请求（5 字节，§166 / §169）。过滤开关：
            # 1 = 待机玩家（客户端进大厅时发的就是这个），0 = 推荐对手。
            flag = int(tokens.pop(0)) if tokens and tokens[0].isdigit() else 1
            page = int(tokens.pop(0)) if tokens and tokens[0].isdigit() else 0
            client.send_game(0x020d, struct.pack("<HHB", page, 18, flag))
            log(f"已发 0x020d 玩家列表请求"
                f"（{'待机玩家' if flag else '推荐对手'}，第 {page} 页）")
            time.sleep(0.4)
        elif cmd == "peer":
            sequence = int(tokens.pop(0)) if tokens and tokens[0].isdigit() else 1
            game_id = int(tokens.pop(0)) if tokens and tokens[0].isdigit() else 0
            seat = client.my_seat if client.my_seat is not None else 0
            blob = build_udp_packet(seat, sequence, game_id)
            log(f"   要发的字节: {blob.hex(' ')}（局号={game_id}）")
            client.send_game(OP_PEER_DATA_UP, blob)
        elif cmd == "rpeer":
            sequence = int(tokens.pop(0)) if tokens and tokens[0].isdigit() else 1
            game_id = int(tokens.pop(0)) if tokens and tokens[0].isdigit() else 0
            if client.relay is None:
                raise SystemExit("还没连上中继（服务端得先回 0x0210）；"
                                 "先用 waitrelay 等一等")
            seat = client.my_seat if client.my_seat is not None else 0
            blob = build_udp_packet(seat, sequence, game_id)
            log(f"   要发的字节: {blob.hex(' ')}（局号={game_id}）")
            client.relay.send_peer(blob)
        elif cmd == "waitrelay":
            seconds = float(tokens.pop(0)) if tokens and _isnum(tokens[0]) else 15.0
            # 真客户端在大厅每帧的 tick 里对每个「别人坐着的座位」10 秒一发
            # （§152）。这里主动发一发，省得干等那 10 秒。
            seat = client.my_seat if client.my_seat is not None else 0
            client.send_game(OP_START_TCP_RELAY,
                             w_i32(seat) + w_i32(1 - seat))
            deadline = time.monotonic() + seconds
            while client.relay is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if client.relay is None:
                raise SystemExit(f"!! {seconds} 秒内没连上中继"
                                 f"（服务端没回 0x0210？加了 --no-tcp-relay？）")
            log("中继已就绪")
        elif cmd == "join":
            room_id = int(tokens.pop(0))
            client.send_game(OP_MOVE_INTO_SESSION,
                             w_i32(room_id) + w_wstr("") + w_i32(0))
            deadline = time.monotonic() + 5.0
            while client.my_seat is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if client.my_seat is None:
                log("!! 5 秒内没等到加入应答")
            else:
                log(f"已进房间 #{client.room_id} 座位 {client.my_seat}")
        elif cmd == "leave":
            client.send_game(OP_LEAVE_SESSION)
            client.my_seat = None
        elif cmd == "chat":
            text = tokens.pop(0)
            client.send_game(OP_CHAT, bytes([0]) + w_wstr(text))
        elif cmd == "team":
            # 「变更队伍」：把自己（或指定座位）那一格的**实况副本**改掉队伍号
            # 再发客户端方向的 0x0301 —— 和真客户端 0x469f95 一模一样。
            new_team = int(tokens.pop(0))
            seat = (int(tokens.pop(0)) if tokens and tokens[0].isdigit()
                    else client.my_seat)
            slot = (client.seats[seat]
                    if seat is not None and 0 <= seat < ROOM_SEAT_COUNT
                    else None)
            if slot is None:
                log("!! 还不知道这个座位长什么样（先 join，等 0x0300）")
            else:
                changed = dict(slot, team=new_team)
                client.send_game(OP_SESSION_MEMBER_UPDATE,
                                 w_i32(seat) + build_session_slot(**changed))
        elif cmd == "roomready":
            # 「游戏准备 / READY」：0x030e，载荷就一个 int32（§165）。
            # ★ 和 `ready`（0x0402 房主开局握手）**不是**一回事。
            value = int(tokens.pop(0)) if tokens and tokens[0].isdigit() else 1
            client.send_game(OP_SEAT_READY, w_i32(value))
        elif cmd == "seats":
            log("座位表：" + (client.describe_seats() or "（还不知道）"))
        # ---- 战斗（J.3）------------------------------------------------
        elif cmd == "ready":
            client.send_game(OP_COUNT_GAME_READY)
        elif cmd == "loaded":
            client.send_game(OP_LOADING_DONE)
        elif cmd == "die":
            deaths = int(tokens.pop(0)) if tokens and tokens[0].isdigit() else 0
            mine = client.my_seat if client.my_seat is not None else 0
            # ★ 第二个参数 = **受害者座位**，用来造「替别人报死亡」的幽灵上报
            #   （bug调查/8）：真客户端每台都模拟全场伤害，射手那台算「炸死了
            #   他」时就会发这么一发。服务端应当只认本人那发、把这发丢掉。
            seat = int(tokens.pop(0)) if tokens and tokens[0].isdigit() else mine
            # 18 字节，紧凑，死亡次数在**线偏移 6**（客户端结构体里在 +0x08，
            # 按后者组包会写坏 X 并让客户端越界崩，V0.1 §108）。
            client.send_game(OP_REPORT_HP_ZERO,
                             struct.pack(DEATH_REPORT_FORMAT,
                                         character_handle(seat), seat & 0xFF,
                                         0xFF, deaths, 100.0, 200.0))
        elif cmd == "respawn":
            seat = client.my_seat if client.my_seat is not None else 0
            client.send_game(OP_REQ_RESPAWN,
                             w_i32(seat) + w_i32(100) + w_i32(200) + w_i32(1))
        elif cmd == "drop":
            item_id = int(tokens.pop(0)) if tokens and tokens[0].isdigit() else 10101
            client.send_game(OP_CREATE_ITEM,
                             struct.pack(CREATE_ITEM_FORMAT, item_id,
                                         100.0, 200.0, 0.0, 0.0, 3, -1, -1))
        elif cmd == "pickup":
            handle = int(tokens.pop(0), 0)
            seat = client.my_seat if client.my_seat is not None else 0
            client.send_game(OP_GET_ITEM, w_i32(seat) + w_i32(handle))
        elif cmd == "useitem":
            slot = int(tokens.pop(0)) if tokens and tokens[0].isdigit() else 0
            client.send_game(OP_USE_ITEM, w_i32(slot))
        elif cmd == "endattr":
            # 0x040d「我身上这个效果结束了」。真客户端在
            # Character::RemoveAttrEffect 里发，只发自己的座位（§200）。
            attr = int(tokens.pop(0)) if tokens and tokens[0].isdigit() else 6
            seat = client.my_seat if client.my_seat is not None else 0
            client.send_game(OP_REMOVE_CHAR_ATTR, w_i32(seat) + w_i32(attr))
        elif cmd == "score":
            client.send_game(OP_UPDATE_QUEST_SCORE, w_i32(int(tokens.pop(0))))
        elif cmd == "cleared":
            client.send_game(OP_MARK_QUEST_SUCCESS, w_i32(1))
        elif cmd == "nextmap":
            client.send_game(OP_REQ_CHANGE_TO_NEXT_MAP, w_wstr(tokens.pop(0)))
        elif cmd == "maploaded":
            client.send_game(OP_MAP_LOADING_DONE)
        elif cmd == "endquest":
            client.send_game(OP_END_QUEST)
        elif cmd == "resultdone":
            client.send_game(OP_LEAVE_RESULT)
        elif cmd in ("sleep", "hold"):
            seconds = float(tokens.pop(0))
            log(f"等 {seconds} 秒")
            time.sleep(seconds)
        elif cmd == "shot":
            pid, path = tokens.pop(0), tokens.pop(0)
            subprocess.run([sys.executable,
                            os.path.join(HERE, "screenshot.py"), pid, path],
                           check=False, capture_output=True)
            log(f"截图 -> {path}")
        elif cmd == "quit":
            log("主动断开（不发 0x0203）")
            return
        else:
            raise SystemExit(f"不认识的命令: {cmd}")


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    host = "127.0.0.1"
    argv = list(argv)
    replay = None
    while argv and argv[0] in ("--host", "--ticket"):
        if argv.pop(0) == "--host":
            host = argv.pop(0)
        else:
            # ★ 拿一张**现成的**票据直接连游戏服，跳过认证服 —— 这正是真客户端
            #   断线重连时干的事（§171）：它反复重放手里那张旧票据，从不回认证服。
            replay = argv.pop(0)
    if len(argv) < 2:
        print(__doc__)
        return 2
    user, password, tokens = argv[0], argv[1], argv[2:]
    if replay:
        log(f"重放已有票据 {replay[:8]}…（不走认证服，模拟断线重连）")
        ticket = replay
    else:
        ticket = get_ticket(host, user, password)
    client = FakeClient(host, ticket)
    try:
        run_script(client, tokens)
        time.sleep(0.5)          # 让最后一发的应答有机会打出来
    finally:
        client.close()
        log("连接已关闭")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
