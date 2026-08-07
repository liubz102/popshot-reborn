#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
authserver.py —— 假认证服（阶段 5 里程碑 A）

监听 127.0.0.1:47611（bshook.dll 把客户端的 connect 重定向过来，端口不变）。
用 protocol.py 完整解帧 / 组帧。

用法：
    python server/authserver.py                 # hold 模式：只收不回，把连接挂住
    python server/authserver.py --reply <plan>  # 按预案回包
    python server/authserver.py --port 47611

hold 模式的用途：让客户端停在「已发登录包、正在等应答」的状态不退出，
便于用 tools/dump_process.py 在那一刻转储进程
（ASProtect 按页惰性解密，登录代码只有跑过之后才在内存里是明文）。
"""
import argparse
import datetime
import os
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import protocol as P
from account_store import AccountStore

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGDIR = os.path.join(ROOT, "logs")
os.makedirs(LOGDIR, exist_ok=True)

_seq = 0
_lock = threading.Lock()


def ts():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log(msg, fh=None):
    line = f"[{ts()}] {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


def make_reply(frame, args):
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
            pay = P.build_login_reply(result=args.result, s1=args.s1, s2=args.s2)
            out.append(P.pack(P.Frame(P.OPCODE_LOGIN_REPLY, pay,
                                      key=frame.key, msg_id=frame.msg_id)))
        elif frame.opcode == P.OPCODE_OLDFASHION:
            pay = P.build_oldfashion_reply(result=args.result)
            out.append(P.pack(P.Frame(P.OPCODE_OLDFASHION_REPLY, pay,
                                      key=frame.key, msg_id=frame.msg_id)))
        elif frame.opcode == P.OPCODE_LOGOUT:
            pay = P.build_logout_reply(result=args.result)
            out.append(P.pack(P.Frame(P.OPCODE_LOGOUT_REPLY, pay,
                                      key=frame.key, msg_id=frame.msg_id)))
        return out
    if args.reply.startswith("raw:"):
        _, op, pay_hex = args.reply.split(":", 2)
        out.append(P.pack(P.Frame(int(op, 0), bytes.fromhex(pay_hex),
                                  key=frame.key, msg_id=frame.msg_id)))
    return out


def handle(conn, addr, args, port=0):
    global _seq
    with _lock:
        _seq += 1
        seq = _seq
    binpath = os.path.join(LOGDIR, f"auth_{seq:03d}_{port}.bin")
    txtpath = os.path.join(LOGDIR, f"auth_{seq:03d}_{port}.txt")
    fb = open(binpath, "wb")
    ft = open(txtpath, "w", encoding="utf-8")
    accounts = AccountStore(args.accounts)
    log(f"+++ 连接#{seq} 端口{port} 来自 {addr[0]}:{addr[1]}", ft)
    buf = b""
    conn.settimeout(None)
    try:
        while True:
            data = conn.recv(8192)
            if not data:
                log(f"连接#{seq} 对端关闭", ft)
                break
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
                ft.write(P.hexdump(f.payload) + "\n")
                if f.opcode == P.OPCODE_LOGIN:
                    try:
                        u, p, tail = P.parse_login(f.payload)
                        log(f"连接#{seq} ★ 登录 用户名={u!r} 密码={p!r} "
                            f"尾部={tail.hex(' ')}", ft)
                        account = accounts.login(u, p)
                        log(f"连接#{seq} ★ 活动账号={u!r} "
                            f"tutorial_completed={account['tutorial_completed']}", ft)
                    except Exception as e:
                        log(f"连接#{seq} 登录包体解析失败: {e}", ft)
                reps = make_reply(f, args)
                for rep in reps:
                    op = struct.unpack_from(">H", rep, 2)[0]
                    log(f"连接#{seq} 回包 opcode=0x{op:04x} 共 {len(rep)} 字节", ft)
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
        log(f"--- 连接#{seq} 结束 -> {binpath}", ft)
        fb.close()
        ft.close()
        try:
            conn.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="47611",
                    help="监听端口，逗号分隔；认证服默认 47611")
    ap.add_argument("--reply", default=None,
                    help="'login' = 回 CULoginReplyPacket；'raw:<opcode>:<hex>' = 手工帧")
    ap.add_argument("--sweep", default=None,
                    help="扫描 opcode 区间，如 '0x08-0x30'，每个都回一帧")
    ap.add_argument("--gap", type=float, default=0.4, help="sweep 时每帧间隔秒数")
    ap.add_argument("--result", type=int, default=0, help="应答里的结果码")
    ap.add_argument("--s1", default="", help="应答字符串1")
    ap.add_argument("--s2", default="", help="应答字符串2")
    ap.add_argument("--accounts", default=None,
                    help="账号 JSON 路径（默认 server/data/accounts.json）")
    args = ap.parse_args()

    ports = [int(x) for x in args.port.replace(" ", "").split(",") if x]
    log(f"[authserver] 监听 127.0.0.1:{ports} "
        f"模式={'回包 ' + args.reply if args.reply else 'hold（只收不回）'}")

    def serve(port):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # 注意：不开 SO_REUSEADDR。开了的话旧进程没退干净时会有两个进程同时
        # LISTEN 同一端口，连接被谁接走看运气 —— 会话 04 踩过这个坑。
        try:
            s.bind(("127.0.0.1", port))
        except OSError as e:
            log(f"!! 端口 {port} 绑定失败（是不是有旧进程没退？）: {e}")
            return
        s.listen(8)
        while True:
            conn, addr = s.accept()
            threading.Thread(target=handle, args=(conn, addr, args, port),
                             daemon=True).start()

    for p in ports:
        threading.Thread(target=serve, args=(p,), daemon=True).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("bye")


if __name__ == "__main__":
    main()
