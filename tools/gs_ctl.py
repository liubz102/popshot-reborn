#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gs_ctl.py —— 假游戏服（server/gameserver.py）的调试控制台

往正在运行的 gameserver 的控制端口（默认 127.0.0.1:27800）发一条命令，
由它转成一个真正的游戏包推给当前连着的客户端。

**为什么需要它**：战斗类应答（`0x0411 gspEndGame` 之类）原本只有在客户端
真把关打完/打输时才会被触发，而闯关模式有强制推进机制
（不向前移动 15 秒就警告，FINDINGS §88），挂机等 12:30 那条测试路线走不通。
有了这个通道，验证一个战斗应答的成本从「打完一整关」降到一行命令。

用法：
    python tools/gs_ctl.py status
    python tools/gs_ctl.py endgame-probe            # 12 个业务值填 101..112
    python tools/gs_ctl.py endgame 0 1 0 0 0 0      # 座位 0 / 成功 / 值全 0
    python tools/gs_ctl.py kill 3c08aad0             # 发 0x0406 死亡广播（句柄见
                                                    # tools/probe_death.py 的 handle）
    python tools/gs_ctl.py respawn                  # 用客户端自报的最后坐标
    python tools/gs_ctl.py sync-account             # 改完 accounts.json 后刷数据栏
    python tools/gs_ctl.py raw 0411 00000000 ...
    python tools/gs_ctl.py help

    python tools/gs_ctl.py --port 27800 status      # 换端口

命令表由服务端持有（`handle_control_command`），这里只负责转发整行。
"""
import argparse
import socket
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def send_command(line, host="127.0.0.1", port=27800, timeout=5.0):
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall((line + "\n").encode("utf-8"))
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", "replace").rstrip("\n")


def main():
    ap = argparse.ArgumentParser(
        description="给正在运行的 gameserver 发调试命令",
        epilog="命令表见服务端的 `help`：python tools/gs_ctl.py help")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=27800)
    ap.add_argument("words", nargs=argparse.REMAINDER,
                    help="要执行的命令，例如 endgame-probe")
    args = ap.parse_args()

    if not args.words:
        ap.print_help()
        return 2

    try:
        print(send_command(" ".join(args.words), args.host, args.port))
    except ConnectionRefusedError:
        print(f"!! 连不上控制端口 {args.host}:{args.port} —— "
              f"gameserver 没在跑？或者启动时带了 --control-port 0")
        return 1
    except OSError as error:
        print(f"!! 控制通道出错: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
