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

它**不渲染任何东西**，只管收发包，所以战斗类的包一概不接（收到就打一行日志）。

用法（命令是一串 token，从左往右顺序执行）：

    python tools/fakeclient.py <账号> <密码> join 1 sleep 5 leave sleep 3
    python tools/fakeclient.py bob pw456 join 1 shot 5964 before.png \\
        leave sleep 2 shot 5964 after.png
    python tools/fakeclient.py bob pw456 join 1 chat "hello" hold 60

命令表：

    create [标题] [类型]  发 0x0201 建一个房间（自己当房主，座位 0）
    join <房间号>     发 0x0202 gcpReqMoveInto，并等应答（打印 result / 座位号）
    leave             发 0x0203 gcpReqLeaveSession
    chat <文本>       发 0x0305 gcpSendChatMsg
    peer [序列号]     发一发 0x040e = 玩家间同步数据（12 字节 UdpPacket 头 +
                      3 字节 body，§149~§151）。用来验服务端有没有原样转成 0x040f
    rpeer [序列号]    同上，但走**原版 TCP 中继**（rcp opcode 3）。要先连上中继
    waitrelay [秒]    等中继连上（服务端回 0x0210 之后才会有），超时就报错退出
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
    CLIENT_VERSION, DESCRIPTOR_SENT_ARGUMENT_COUNTS,
    GCP_NAMES, MAGIC_CTRL, MAGIC_GAME, OP_CHAT,
    OP_JOIN_RELAY, OP_LEAVE_RELAY, OP_START_TCP_RELAY,
    OP_LEAVE_SESSION, OP_MOVE_INTO_SESSION, OP_PEER_DATA_DOWN,
    OP_PEER_DATA_UP, OP_SESSION_MEMBER_UPDATE, OP_SESSION_MEMBERS,
    OP_TOGGLE_PEER_RELAY, Reader, build_game, describe_peer_header,
    take_frame, w_i32, w_wstr,
)

AUTH_PORT = 47611
GAME_PORT = 27799

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
        elif opcode == OP_JOIN_RELAY and len(payload) >= 18:
            self._join_relay(payload)
        elif opcode == OP_LEAVE_RELAY:
            if self.relay is not None:
                log("[中继] 收到 0x0211 —— 拆掉中继连接（走析构，不是断线）")
                self.relay.close()
                self.relay = None

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


def describe(opcode, payload):
    """把最关心的几个包解出人能读的一行 —— 其余的只报长度。"""
    try:
        if opcode == OP_SESSION_MEMBER_UPDATE and len(payload) >= 5:
            action = payload[0]
            seat = struct.unpack_from("<i", payload, 1)[0]
            what = {0: "加入(建模型)", 1: "离开(销毁模型)", 2: "离开(销毁模型)",
                    3: "重建模型", 4: "换角色"}.get(action, "未知")
            return f"  ★ 座位变更 action={action}（{what}）座位={seat}"
        if opcode == OP_SESSION_MEMBERS and len(payload) >= 8:
            return f"  房主座位={struct.unpack_from('<i', payload)[0]}"
        if opcode == OP_MOVE_INTO_SESSION and len(payload) >= 12:
            result, room_id, seat = struct.unpack_from("<iii", payload)
            return f"  加入结果={result} 房间={room_id} 我的座位={seat}"
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
            client.send_game(0x0201,
                             w_wstr(title) + w_wstr("") + w_wstr("")
                             + w_i32(0) + w_i32(session_type)
                             + b"".join(w_i32(v) for v in (3, 1, 0)[:count]))
            client.my_seat, client.room_id = 0, None
            time.sleep(0.5)
            log(f"已建房「{title}」type={session_type}（我是房主，座位 0）")
        elif cmd == "peer":
            sequence = int(tokens.pop(0)) if tokens and tokens[0].isdigit() else 1
            seat = client.my_seat if client.my_seat is not None else 0
            # 12 字节 UdpPacket 头：magic / 发送方座位 / 目标座位(0xff 广播) /
            # ? / 局号 / 校验和 / 序列号 / 内层 opcode（§151）
            blob = (struct.pack("<BbbB", 0xFF, seat, -1, 0)
                    + struct.pack("<HHHH", 0, 0x1234, sequence, 0x0102)
                    + b"\xde\xad\xbe")
            log(f"   要发的字节: {blob.hex(' ')}")
            client.send_game(OP_PEER_DATA_UP, blob)
        elif cmd == "rpeer":
            sequence = int(tokens.pop(0)) if tokens and tokens[0].isdigit() else 1
            if client.relay is None:
                raise SystemExit("还没连上中继（服务端得先回 0x0210）；"
                                 "先用 waitrelay 等一等")
            seat = client.my_seat if client.my_seat is not None else 0
            blob = (struct.pack("<BbbB", 0xFF, seat, -1, 0)
                    + struct.pack("<HHHH", 0, 0x1234, sequence, 0x0102)
                    + b"\xde\xad\xbe")
            log(f"   要发的字节: {blob.hex(' ')}")
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
    if argv[0] == "--host":
        argv.pop(0)
        host = argv.pop(0)
    user, password, tokens = argv[0], argv[1], argv[2:]
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
