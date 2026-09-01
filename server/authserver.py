#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
authserver.py —— 认证服（NMCO 协议，端口 47611）

**V0.2 起它不再是「谁来都放行」的假后台**：

* 校验用户名 / 密码，三态分明（成功 / 未注册 / 密码错），失败时回**客户端认识的**
  NMCO 错误码（20025 / 20026），登录框就会自己弹出「不存在的帐号」/「密码错误」
  （FINDINGS §128）；
* 成功时签发一张**票据**塞进 `CULoginReplyPacket` 的字符串字段，
  客户端会把它原样转发给游戏服的 `gcpReqLogin`，游戏服据此定账号
  （FINDINGS §123，决策 D064）；
* **日志里绝不打印密码**（D067），只打用户名和票据前 8 位。

正常情况下由 `server/app.py` 和游戏服跑在同一个进程里（票据表要共享）。
单独跑本文件仍然可以，用于协议试探：

    python server/authserver.py                 # hold 模式：只收不回，把连接挂住
    python server/authserver.py --reply login   # 正常应答
    python server/authserver.py --port 47611

hold 模式的用途：让客户端停在「已发登录包、正在等应答」的状态不退出，
便于用 tools/dump_process.py 在那一刻转储进程。
"""
import argparse
import datetime
import os
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import protocol as P
from account_store import (AUTH_BAD_PASSWORD, AUTH_MESSAGES, AUTH_NO_SUCH_USER,
                           AUTH_OK, AccountStore)
import asynclog
import logcleanup
import eventlog
from netlisten import create_listener, tune_stream
from tickets import TicketStore, short as short_ticket

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGDIR = os.path.join(ROOT, "logs")
os.makedirs(LOGDIR, exist_ok=True)

#: `CULoginReplyPacket` 的结果码。
#:
#: ★ **这三个数不是随便编的，是客户端认识的 NMCO 错误码**（FINDINGS §128）。
#: `nmconew.dll` 的 `0x10077000` 把包里的结果码映射成 NM 错误码，客户端再拿它
#: 去 `Data/Chinese.ini` 查中文，最后弹 `MessageBoxW("<中文> (<码>)", "登录失败")`：
#:
#:     20025 -> 「不存在的帐号」        20026 -> 「密码错误」
#:     其它任何非零值 -> 20000「认证服务器失败」（= 一句没用的笼统话）
#:
#: 所以这里**必须发 20025 / 20026**，客户端才会自己说人话。别再改回 1 / 2。
LOGIN_RESULT_OK = 0
LOGIN_RESULT_NO_SUCH_USER = 20025
LOGIN_RESULT_BAD_PASSWORD = 20026
#: 登录包都解不开时用它 —— 这不是账号的问题，「认证服务器失败」才是实话。
LOGIN_RESULT_SERVER_ERROR = 20000

_AUTH_RESULT_CODES = {
    AUTH_OK: LOGIN_RESULT_OK,
    AUTH_NO_SUCH_USER: LOGIN_RESULT_NO_SUCH_USER,
    AUTH_BAD_PASSWORD: LOGIN_RESULT_BAD_PASSWORD,
}

_seq = 0
_lock = threading.Lock()

#: 逐包 hexdump / 落盘。默认关：云端服务器长期跑，不该给每条连接留两个文件。
VERBOSE = False


def ts():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log(msg, fh=None):
    line = f"[{ts()}] {msg}"
    asynclog.emit(line)
    if fh:
        asynclog.emit_text(line + "\n", fh)


class AuthService:
    """账号校验 + 票据签发。认证服和游戏服共用同一个实例（同一个进程内）。"""

    def __init__(self, accounts=None, tickets=None):
        # ★ 用 `is None` 不用 `or`：`TicketStore` 实现了 `__len__`，空表的
        #   布尔值是 False，`tickets or TicketStore()` 会**悄悄换掉**传进来的
        #   那张表，认证服签的票据游戏服就查不到了。
        self.accounts = accounts if accounts is not None else AccountStore()
        self.tickets = tickets if tickets is not None else TicketStore()

    def login(self, username, password):
        """返回 ``(结果码, 票据, 给玩家看的中文, 三态状态)``。"""
        status, _account = self.accounts.verify(username, password)
        message = AUTH_MESSAGES[status]
        if status != AUTH_OK:
            return _AUTH_RESULT_CODES[status], "", message, status
        ticket = self.tickets.issue(str(username).strip())
        return LOGIN_RESULT_OK, ticket, message, status


def make_reply(frame, args, service, ft=None, seq=0, peer="?"):
    """给一帧请求造应答。返回 bytes 列表（可能多帧）或 []。

    `--reply login` 走正规路子：收到 opcode 0x0f（CULogin2Packet）就回
    opcode 0x0c（CULoginReplyPacket），字段按 nmconew.dll 的反序列化顺序填。
    `--reply raw:<opcode>:<载荷hex>` 用于手工试探。
    `--sweep A-B` 把 opcode A..B 全部各回一帧，用来定位客户端到底认哪个码。
    """
    out = []
    if args.sweep:
        lo, _, hi = args.sweep.partition("-")
        for op in range(int(lo, 0), int(hi, 0) + 1):
            pay = P.build_login_reply(result=args.result)
            out.append(P.pack(P.Frame(op, pay, key=frame.key, msg_id=frame.msg_id)))
        return out
    if not args.reply:
        return out
    if args.reply == "login":
        if frame.opcode == P.OPCODE_LOGIN:
            result, ticket, message = _handle_login_frame(frame, args, service,
                                                          ft, seq, peer)
            # ★ 票据必须放**第二个**字符串（s2）—— 实测客户端转发给
            #   `gcpReqLogin` 的就是它（FINDINGS §123）。第一个字符串留给中文说明。
            s1, s2 = message, ticket
            if getattr(args, "ticket_field", "s2") == "s1":
                s1, s2 = ticket, message
            pay = P.build_login_reply(result=result, s1=s1, s2=s2)
            out.append(P.pack(P.Frame(P.OPCODE_LOGIN_REPLY, pay,
                                      key=frame.key, msg_id=frame.msg_id)))
        elif frame.opcode == P.OPCODE_OLDFASHION:
            pay = P.build_oldfashion_reply(result=0)
            out.append(P.pack(P.Frame(P.OPCODE_OLDFASHION_REPLY, pay,
                                      key=frame.key, msg_id=frame.msg_id)))
        elif frame.opcode == P.OPCODE_LOGOUT:
            pay = P.build_logout_reply(result=0)
            out.append(P.pack(P.Frame(P.OPCODE_LOGOUT_REPLY, pay,
                                      key=frame.key, msg_id=frame.msg_id)))
        return out
    if args.reply.startswith("raw:"):
        _, op, pay_hex = args.reply.split(":", 2)
        out.append(P.pack(P.Frame(int(op, 0), bytes.fromhex(pay_hex),
                                  key=frame.key, msg_id=frame.msg_id)))
    return out


def _handle_login_frame(frame, args, service, ft, seq, peer="?"):
    """解 `CULogin2Packet` 并做认证，返回 ``(结果码, 票据, 中文说明)``。"""
    try:
        username, password, tail = P.parse_login(frame.payload)
    except Exception as error:
        log(f"连接#{seq} 登录包体解析失败: {error}", ft)
        return LOGIN_RESULT_SERVER_ERROR, "", "登录数据无法解析，请重试"
    # ★ 只打用户名，绝不打密码（D067）。尾部字节对排查有用，留着。
    log(f"连接#{seq} ★ 登录请求 用户名={username!r} 密码长度={len(password)} "
        f"尾部={tail.hex(' ')}", ft)
    if args.allow_any:
        # 只给协议试探用：跳过校验，直接给活动票据。
        ticket = service.tickets.issue(str(username).strip() or "local")
        log(f"连接#{seq} ⚠ --allow-any：跳过校验，票据={short_ticket(ticket)}", ft)
        eventlog.online(f"认证服 #{seq} ⚠ 免密放行 账号={username!r} ip={peer}")
        return LOGIN_RESULT_OK, ticket, AUTH_MESSAGES[AUTH_OK]
    result, ticket, message, status = service.login(username, password)
    if status == AUTH_OK:
        log(f"连接#{seq} ★ 认证通过 用户名={username!r} "
            f"票据={short_ticket(ticket)}", ft)
        eventlog.online(f"认证服 #{seq} ✓ 认证通过 账号={username!r} ip={peer}")
    else:
        log(f"连接#{seq} ✗ 认证失败 用户名={username!r} 原因={status}（{message}）", ft)
        eventlog.online(f"认证服 #{seq} ✗ 认证失败 账号={username!r} ip={peer} "
                        f"原因={status}（{message}）")
    return result, ticket, message


def handle(conn, addr, args, port=0, service=None):
    global _seq
    with _lock:
        _seq += 1
        seq = _seq
    service = service or AuthService(AccountStore(args.accounts))
    ft = fb = None
    if VERBOSE:
        # ★ 名字里带**本次启动的时刻**（`logcleanup.RUN_STAMP`）：序号是进程级的、
        #   每次启动从 1 重来，不带它的话同一天第二次启动就把上一次的抓包冲掉
        #   （用户 2026-09-01）。仍然匹配 `auth_*` 白名单，到期照样清。
        stem = f"auth_{logcleanup.RUN_STAMP}_{seq:03d}_{port}"
        fb = open(os.path.join(LOGDIR, f"{stem}.bin"), "wb")
        ft = open(os.path.join(LOGDIR, f"{stem}.txt"), "w", encoding="utf-8")
    log(f"+++ 连接#{seq} 端口{port} 来自 {addr[0]}:{addr[1]}", ft)
    peer = eventlog.peer(addr)
    buf = b""
    conn.settimeout(None)
    try:
        while True:
            data = conn.recv(8192)
            if not data:
                log(f"连接#{seq} 对端关闭", ft)
                break
            if fb:
                fb.write(data)
                fb.flush()
            buf += data
            while True:
                n = P.frame_len(buf)
                if n is None or len(buf) < n:
                    break
                raw, buf = buf[:n], buf[n:]
                try:
                    f = P.unpack(raw)
                except Exception as e:
                    log(f"连接#{seq} 解帧失败 {e}\n{P.hexdump(raw)}", ft)
                    continue
                log(f"连接#{seq} 收到 {f}", ft)
                if VERBOSE and ft:
                    ft.write(P.hexdump(f.payload) + "\n")
                reps = make_reply(f, args, service, ft, seq, peer)
                for rep in reps:
                    op = struct.unpack_from(">H", rep, 2)[0]
                    log(f"连接#{seq} 回包 opcode=0x{op:04x} 共 {len(rep)} 字节", ft)
                    if VERBOSE and ft:
                        ft.write(P.hexdump(rep) + "\n")
                    conn.sendall(rep)
                    if args.sweep:
                        time.sleep(args.gap)
                if not reps:
                    log(f"连接#{seq} [hold] 不回包，挂住连接", ft)
    except ConnectionResetError:
        log(f"连接#{seq} 被对端重置", ft)
    except Exception as e:
        log(f"连接#{seq} 异常: {e!r}", ft)
    finally:
        log(f"--- 连接#{seq} 结束", ft)
        for fh in (fb, ft):
            if fh:
                fh.close()
        try:
            conn.close()
        except Exception:
            pass


def serve(port, args, service, host="::", ready=None):
    """在 `port` 上接受连接（阻塞）。`app.py` 会把它丢进一个线程。

    监听默认 ``::`` 双栈，细节见 `netlisten.create_listener`。
    """
    s = create_listener(host, port)
    if ready is not None:
        ready.set()
    while True:
        conn, addr = s.accept()
        # 认证服的包也小、也是一问一答，关 Nagle 只会让登录更快（D104）。
        tune_stream(conn)
        threading.Thread(target=handle, args=(conn, addr, args, port, service),
                         daemon=True).start()


def build_arg_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="47611",
                    help="监听端口，逗号分隔；认证服默认 47611")
    ap.add_argument("--host", default="::", help="监听地址，默认 :: （双栈）")
    ap.add_argument("--reply", default=None,
                    help="'login' = 正常认证并回 CULoginReplyPacket；"
                         "'raw:<opcode>:<hex>' = 手工帧")
    ap.add_argument("--sweep", default=None,
                    help="扫描 opcode 区间，如 '0x08-0x30'，每个都回一帧")
    ap.add_argument("--gap", type=float, default=0.4, help="sweep 时每帧间隔秒数")
    ap.add_argument("--result", type=int, default=0,
                    help="sweep / raw 模式下应答里的结果码")
    ap.add_argument("--ticket-field", choices=("s1", "s2"), default="s2",
                    help="票据放在 CULoginReplyPacket 的哪个字符串字段。"
                         "默认 s2 —— 实测客户端转发给 gcpReqLogin 的就是它"
                         "（FINDINGS §123）；s1 只留给回归排查")
    ap.add_argument("--allow-any", action="store_true",
                    help="⚠ 只给协议试探：跳过密码校验，任何账号都放行")
    ap.add_argument("--accounts", default=None,
                    help="账号 JSON 路径（默认 server/data/accounts.json）")
    ap.add_argument("--verbose", action="store_true",
                    help="逐包 hexdump + 抓包落盘（每条连接两个文件）")
    return ap


def main():
    args = build_arg_parser().parse_args()

    global VERBOSE
    VERBOSE = args.verbose

    ports = [int(x) for x in str(args.port).replace(" ", "").split(",") if x]
    service = AuthService(AccountStore(args.accounts))
    log(f"[authserver] 监听 [{args.host}]:{ports} "
        f"模式={'认证 ' + args.reply if args.reply else 'hold（只收不回）'}")

    for p in ports:
        threading.Thread(target=serve, args=(p, args, service, args.host),
                         daemon=True).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("bye")


if __name__ == "__main__":
    main()
